"""Security properties no earlier test would notice losing (survivors of the mutation round)."""

import asyncio
import hashlib
import json
import ssl
from unittest.mock import Mock, sentinel

import httpx
import pytest
from starlette.responses import PlainTextResponse
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from app.core.auth import SessionStore
from app.core.host_validation import WEBHOOK_PATH, HostValidationMiddleware
from app.services.umay import codex_bridge_policy as policy
from app.services.umay import codex_bridge_tls as tls
from app.services.umay.codex_bridge_filter import MAX_CLIENT_FRAME_BYTES
from scripts import codex_bridge_front as front

SECRET = "bridge-secret-" + "1nv4r14nt-" * 6
INNER = "inner-" + "1nv4r14nt" * 6
MARKER = "verified-invariants"


def test_a_session_id_is_kept_only_as_its_digest():
    store = SessionStore()
    session_id = store.issue()
    assert session_id not in repr(vars(store))
    assert hashlib.sha256(session_id.encode()).hexdigest() in store._expiry
    assert store.valid(session_id)


def test_a_session_digest_is_not_itself_accepted():
    store = SessionStore()
    session_id = store.issue()
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    assert store.valid(digest) is False


def test_client_tls_floor_without_a_listener():
    assert tls.client_context(SECRET).minimum_version == ssl.TLSVersion.TLSv1_3


def test_front_passes_the_allowed_frame_size_to_the_listener(monkeypatch):
    listener = Mock(return_value=sentinel.listener)
    monkeypatch.setattr(front, "serve", listener)
    instance = front.Front({"inner_port": 1, "marker": MARKER}, SECRET, INNER)
    assert instance.listen("127.0.0.1", 0, tls.client_context(SECRET)) is sentinel.listener
    assert listener.call_args.kwargs["max_size"] == MAX_CLIENT_FRAME_BYTES


@pytest.mark.usefixtures("local_sockets")
async def test_the_applications_client_refuses_tls_below_1_3(tmp_path):
    """A server able to speak only TLS 1.2, holding the right key: the floor alone refuses it."""
    context = tls.server_context(SECRET, tmp_path)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    reached: list = []

    async def handler(connection):
        reached.append(connection.request.headers.get("Authorization"))

    async with serve(handler, "127.0.0.1", 0, ssl=context) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises((ssl.SSLError, ConnectionError, TimeoutError)):
            async with connect(
                f"wss://127.0.0.1:{port}",
                ssl=tls.client_context(SECRET),
                additional_headers={"Authorization": f"Bearer {SECRET}"},
                proxy=None,
                open_timeout=5,
            ):
                pytest.fail("a TLS 1.2 server must not be accepted")
    assert reached == []


async def _app_server(connection):
    async for raw in connection:
        message = json.loads(raw)
        if "id" not in message:
            continue
        result = {}
        if message.get("method") == "config/read":
            result = {"config": {"model_catalog_json": f"/srv/{MARKER}/catalog.json"}}
        await connection.send(json.dumps({"id": message["id"], "result": result}))


@pytest.mark.usefixtures("local_sockets")
async def test_the_front_reads_a_frame_of_exactly_the_allowed_size(tmp_path):
    """A lower ``max_size`` (the library default is 1 MiB) would cut the allowed frames off."""
    async with serve(_app_server, "127.0.0.1", 0) as inner:
        config = {"inner_port": inner.sockets[0].getsockname()[1], "marker": MARKER}
        instance = front.Front(config, SECRET, INNER)
        context = tls.server_context(SECRET, tmp_path)
        async with await instance.listen("127.0.0.1", 0, context) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(
                f"wss://127.0.0.1:{port}",
                ssl=tls.client_context(SECRET),
                additional_headers={"Authorization": f"Bearer {SECRET}"},
                proxy=None,
                open_timeout=10,
                max_size=None,
                close_timeout=5,
            ) as chat:
                opening = {"id": 1, "method": "initialize", "params": policy.initialize_params()}
                await chat.send(json.dumps(opening))
                await asyncio.wait_for(chat.recv(), 10)
                template = json.dumps({"pad": ""})
                await chat.send(json.dumps({"pad": "x" * (MAX_CLIENT_FRAME_BYTES - len(template))}))
                with pytest.raises(ConnectionClosed) as ended:
                    await asyncio.wait_for(chat.recv(), 20)
    # The allowlist refused it (1008). 1009 would mean the frame never got that far.
    assert ended.value.rcvd.code == 1008


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (f"https://hooks.example{WEBHOOK_PATH}", 200),
        (f"http://hooks.example{WEBHOOK_PATH}", 400),
    ],
)
async def test_only_an_https_webhook_endpoint_opens_a_public_host(endpoint, expected):
    async def application(scope, receive, send):
        await PlainTextResponse("reached")(scope, receive, send)

    wrapped = HostValidationMiddleware(application, endpoint)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="https://hooks.example"
    ) as client:
        response = await client.post(WEBHOOK_PATH)
    assert response.status_code == expected
