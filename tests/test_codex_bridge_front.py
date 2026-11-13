"""The bridge front over real TLS sockets: a fake app-server sees only what the chat may send."""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from app.services.umay import codex_bridge_policy as policy
from app.services.umay import codex_bridge_tls as tls
from app.services.umay.codex_bridge_filter import MAX_CLIENT_FRAME_BYTES
from app.services.umay.codex_chat_thread import chat_thread_params
from scripts import codex_bridge_front as front

SECRET = "bridge-secret-" + "f4k3-" * 12
INNER = "inner-" + "t0k3n" * 10
MARKER = "verified-abc"
pytestmark = pytest.mark.usefixtures("local_sockets")


class FakeAppServer:
    """Records every frame of the chat connections and answers from a script."""

    def __init__(self, marker: str = MARKER):
        self.marker = marker
        self.frames: list[dict] = []
        self.bearers: list[str | None] = []

    async def handler(self, connection):
        chat = True
        async for raw in connection:
            message = json.loads(raw)
            method = message.get("method")
            if (
                method == "initialize"
                and message["params"]["clientInfo"]["name"] != "healthos_chat"
            ):
                chat = False  # the front's own proof of this listener, not a chat frame
            if chat:
                self.bearers.append(connection.request.headers.get("Authorization"))
                self.frames.append(message)
            if "id" not in message or "method" not in message:
                continue
            await connection.send(json.dumps({"id": message["id"], "result": self.result(method)}))

    def result(self, method):
        if method == "config/read":
            return {"config": {"model_catalog_json": f"/srv/{self.marker}/catalog.json"}}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        return {}


@pytest.fixture
async def bridge(tmp_path):
    inner = FakeAppServer()
    async with serve(inner.handler, "127.0.0.1", 0) as inner_server:
        config = {"inner_port": inner_server.sockets[0].getsockname()[1], "marker": MARKER}
        instance = front.Front(config, SECRET, INNER)
        async with await instance.listen(
            "127.0.0.1", 0, tls.server_context(SECRET, tmp_path)
        ) as server:
            yield instance, server.sockets[0].getsockname()[1], inner


def dial(port, headers=None, **options):
    return connect(
        f"wss://127.0.0.1:{port}",
        ssl=tls.client_context(SECRET),
        additional_headers=headers
        if headers is not None
        else {"Authorization": f"Bearer {SECRET}"},
        open_timeout=10,
        **options,
    )


async def request(socket_, request_id, method, params):
    await socket_.send(json.dumps({"id": request_id, "method": method, "params": params}))
    return json.loads(await asyncio.wait_for(socket_.recv(), 10))


async def open_chat(socket_):
    assert "result" in await request(socket_, 1, "initialize", policy.initialize_params())
    await socket_.send(json.dumps({"method": "initialized", "params": {}}))


def methods(inner):
    return [frame.get("method") for frame in inner.frames]


async def closed_with(socket_):
    with pytest.raises(ConnectionClosed) as ended:
        await asyncio.wait_for(socket_.recv(), 10)
    return ended.value.rcvd.code


async def test_the_chat_passes_with_the_inner_bearer_and_never_the_bridge_token(bridge):
    _, port, inner = bridge
    async with dial(port) as chat:
        await open_chat(chat)
        assert "result" in await request(chat, 2, "account/read", {})
        assert "result" in await request(chat, 3, "config/read", {})
        started = await request(chat, 4, "thread/start", chat_thread_params())
        assert started["result"]["thread"]["id"] == "thread-1"
    assert methods(inner) == [
        "initialize",
        "initialized",
        "account/read",
        "config/read",
        "thread/start",
    ]
    assert set(inner.bearers) == {f"Bearer {INNER}"}


async def test_a_bridge_token_holder_cannot_run_a_command(bridge):
    _, port, inner = bridge
    attack = {"command": ["true"], "sandboxPolicy": {"type": "dangerFullAccess"}}
    async with dial(port) as chat:
        await open_chat(chat)
        refused = await request(chat, 2, "command/exec", attack)
        assert refused["id"] == 2 and "error" in refused
        assert await closed_with(chat) == 1008
    assert "command/exec" not in methods(inner)


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        json.dumps([{"id": 2, "method": "account/read", "params": {}}]),
        '{"id":2,"method":"account/read","method":"command/exec","params":{}}',
        b"\x00binary",
    ],
)
async def test_frames_the_chat_never_sends_end_the_connection(bridge, raw):
    _, port, inner = bridge
    async with dial(port) as chat:
        await open_chat(chat)
        await chat.send(raw)
        assert await closed_with(chat) == 1008
    assert methods(inner) == ["initialize", "initialized"]


async def test_a_refused_thread_ends_the_connection(bridge):
    _, port, inner = bridge
    async with dial(port) as chat:
        await open_chat(chat)
        changed = {**chat_thread_params(), "sandbox": "danger-full-access"}
        assert "error" in await request(chat, 2, "thread/start", changed)
        assert await closed_with(chat) == 1008
    assert "thread/start" not in methods(inner)


async def test_an_oversized_frame_never_reaches_the_app_server(bridge):
    _, port, inner = bridge
    async with dial(port, max_size=None) as chat:
        await open_chat(chat)
        await chat.send(json.dumps({"pad": "x" * (MAX_CLIENT_FRAME_BYTES + 1)}))
        assert await closed_with(chat) == 1009
    assert methods(inner) == ["initialize", "initialized"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {SECRET}"},
        {"Authorization": f"Token {SECRET}"},
        {"Authorization": SECRET},
    ],
)
async def test_only_the_bearer_scheme_opens_the_bridge(bridge, headers):
    _, port, inner = bridge
    with pytest.raises(InvalidStatus) as refused:
        async with dial(port, headers=headers):
            pytest.fail("the connection must not open")
    assert refused.value.response.status_code == 401
    assert inner.frames == []


async def test_a_listener_that_is_not_the_app_server_gets_no_chat(tmp_path):
    inner = FakeAppServer(marker="public-guess")
    async with serve(inner.handler, "127.0.0.1", 0) as inner_server:
        config = {"inner_port": inner_server.sockets[0].getsockname()[1], "marker": MARKER}
        instance = front.Front(config, SECRET, INNER)
        async with await instance.listen(
            "127.0.0.1", 0, tls.server_context(SECRET, tmp_path)
        ) as server:
            with pytest.raises(InvalidStatus) as refused:
                async with dial(server.sockets[0].getsockname()[1]):
                    pytest.fail("the connection must not open")
    assert refused.value.response.status_code == 503
    assert inner.frames == []


async def test_connections_above_the_cap_are_dropped(bridge, monkeypatch):
    instance, port, _ = bridge
    monkeypatch.setattr(front, "MAX_CONNECTIONS", 2)
    async with dial(port), dial(port):
        assert instance.connections == 2
        with pytest.raises((InvalidHandshake, ConnectionClosed, OSError, TimeoutError)):
            async with dial(port):
                pytest.fail("a connection above the cap must not open")
    await asyncio.sleep(0)
    for _ in range(100):
        if instance.connections == 0:
            break
        await asyncio.sleep(0.02)  # the transports of the closed clients are being released
    async with dial(port):  # the cap counts open connections, not all there ever were
        assert instance.connections == 1


async def test_a_connection_that_never_finishes_its_request_is_tracked(bridge):
    instance, port, _ = bridge
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=tls.client_context(SECRET), server_hostname="healthos-bridge"
    )
    try:
        for _ in range(100):
            if instance.waiting == 1:
                break
            await asyncio.sleep(0.02)
        assert instance.waiting == 1
        assert instance.connections == 0  # waiting does not use up a chat's place
    finally:
        writer.close()
    await reader.read()


class ScriptedSocket:
    def __init__(self, frames):
        self.frames = [json.dumps(frame) for frame in frames]

    async def recv(self):
        return self.frames.pop(0)


async def test_inner_proof_waits_for_its_own_reply():
    reply = {"id": 7, "result": {"ok": True}}
    frames = [
        {"id": 7, "method": "server/request"},
        {"method": "x"},
        {"id": 8, "result": {}},
        reply,
    ]
    assert await front._reply(ScriptedSocket(frames), 7) == reply
