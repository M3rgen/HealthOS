"""Sockets that never present the bearer cannot keep a real caller out of the bridge front."""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from app.services.umay import codex_bridge_policy as policy
from app.services.umay import codex_bridge_tls as tls
from scripts import codex_bridge_front as front

SECRET = "bridge-secret-" + "4dm1ss10n-" * 6
INNER = "inner-" + "4dm1ss10n" * 6
MARKER = "verified-admission"
pytestmark = pytest.mark.usefixtures("local_sockets")


async def app_server(connection):
    async for raw in connection:
        message = json.loads(raw)
        if "id" not in message:
            continue
        result = {}
        if message.get("method") == "config/read":
            result = {"config": {"model_catalog_json": f"/srv/{MARKER}/catalog.json"}}
        await connection.send(json.dumps({"id": message["id"], "result": result}))


@pytest.fixture
async def bridge(tmp_path):
    async with serve(app_server, "127.0.0.1", 0) as inner:
        config = {"inner_port": inner.sockets[0].getsockname()[1], "marker": MARKER}
        instance = front.Front(config, SECRET, INNER)
        context = tls.server_context(SECRET, tmp_path)
        async with await instance.listen("127.0.0.1", 0, context) as server:
            yield instance, server.sockets[0].getsockname()[1]


def dial(port):
    return connect(
        f"wss://127.0.0.1:{port}",
        ssl=tls.client_context(SECRET),
        additional_headers={"Authorization": f"Bearer {SECRET}"},
        proxy=None,
        open_timeout=4,
    )


async def silent_socket(port):
    """TLS finished, then nothing: the opening request never comes."""
    return await asyncio.open_connection(
        "127.0.0.1", port, ssl=tls.client_context(SECRET), server_hostname="healthos-bridge"
    )


async def until(condition, seconds=4.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


async def is_cut_off(reader, seconds):
    try:
        async with asyncio.timeout(seconds):
            return await reader.read() == b""
    except TimeoutError:
        return False
    except OSError:
        return True  # aborted: the connection is gone, which is the point


async def chat_works(port):
    async with dial(port) as chat:
        opening = {"id": 1, "method": "initialize", "params": policy.initialize_params()}
        await chat.send(json.dumps(opening))
        return json.loads(await asyncio.wait_for(chat.recv(), 4)) == {"id": 1, "result": {}}


async def close_all(sockets):
    for _, writer in sockets:
        writer.close()


async def test_silent_sockets_at_the_old_cap_do_not_lock_out_a_real_caller(bridge):
    _, port = bridge
    flood = [await silent_socket(port) for _ in range(16)]
    try:
        assert await chat_works(port)
    finally:
        await close_all(flood)


async def test_a_full_waiting_room_gives_way_to_the_newest_connection(bridge, monkeypatch):
    instance, port = bridge
    monkeypatch.setattr(front, "MAX_UNAUTHENTICATED", 2)
    first = await silent_socket(port)
    second = await silent_socket(port)
    await until(lambda: instance.waiting == 2)
    third = await silent_socket(port)
    try:
        assert await is_cut_off(first[0], 4), "the oldest waiting socket makes room"
        await until(lambda: instance.waiting == 2)
        assert not await is_cut_off(second[0], 0.05)
    finally:
        await close_all([first, second, third])


async def test_a_socket_that_stays_silent_is_cut_off_quickly(bridge, monkeypatch):
    _, port = bridge
    monkeypatch.setattr(front, "UNAUTHENTICATED_SECONDS", 0.1, raising=False)
    reader, writer = await silent_socket(port)
    try:
        assert await is_cut_off(reader, 4)
    finally:
        writer.close()


async def test_a_request_that_trickles_in_is_cut_off_as_well(bridge, monkeypatch):
    _, port = bridge
    monkeypatch.setattr(front, "UNAUTHENTICATED_SECONDS", 0.1, raising=False)
    reader, writer = await silent_socket(port)
    try:
        writer.write(b"GET / HTTP/1.1\r\nHost: bridge\r\n")
        await writer.drain()
        assert await is_cut_off(reader, 4)
    finally:
        writer.close()


async def test_the_app_server_proof_is_not_charged_to_the_request_timeout(bridge, monkeypatch):
    instance, port = bridge
    monkeypatch.setattr(front, "UNAUTHENTICATED_SECONDS", 0.1, raising=False)
    original = instance.genuine

    async def slow_proof():
        await asyncio.sleep(0.4)
        return await original()

    monkeypatch.setattr(instance, "genuine", slow_proof)
    assert await chat_works(port)


async def test_waiting_sockets_do_not_use_up_the_chats_and_chats_do_not_wait(bridge, monkeypatch):
    instance, port = bridge
    monkeypatch.setattr(front, "MAX_CONNECTIONS", 1)
    silent = [await silent_socket(port), await silent_socket(port)]
    await until(lambda: instance.waiting == 2)
    try:
        async with dial(port):
            assert instance.connections == 1
            assert instance.waiting == 2
    finally:
        await close_all(silent)
    await until(lambda: instance.waiting == 0 and instance.connections == 0)
