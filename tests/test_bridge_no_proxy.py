"""Every bridge connection dials directly: a proxy from the environment sees nothing.

The Codex process needs HTTPS_PROXY for its provider traffic, so the front process inherits it
(app/services/codex_runtime_rpc.py). ``websockets`` follows that variable by default, which would
hand the proxy the private inner token and every chat frame. Real sockets, no real CLI.
"""

import ast
import asyncio
import json
import socket
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from support.auth import BASE, TOKEN, build
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from app.services.umay import codex_bridge_policy as policy
from app.services.umay import codex_bridge_tls as tls
from scripts import codex_bridge_front as front
from scripts import start_codex_bridge as launcher

ROOT = Path(__file__).resolve().parents[1]
SECRET = "bridge-secret-" + "n0-pr0xy-" * 6
INNER = "inner-" + "n0pr0xy" * 8
MARKER = "verified-direct"
pytestmark = pytest.mark.usefixtures("local_sockets")


class ProxySink:
    """A listener that only counts who came to it: any connection here is the failure."""

    def __init__(self):
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.connections = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self):
        while not self.stopping.is_set():
            try:
                peer, _ = self.socket.accept()
            except OSError:
                continue
            self.connections += 1
            peer.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopping.set()
        self.thread.join(timeout=5)
        self.socket.close()


@pytest.fixture
def proxied(monkeypatch):
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    with ProxySink() as sink:
        url = f"http://127.0.0.1:{sink.port}"
        for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(name, url)
        yield sink


async def app_server(connection):
    async for raw in connection:
        message = json.loads(raw)
        if "id" not in message:
            continue
        result = {}
        if message.get("method") == "config/read":
            result = {"config": {"model_catalog_json": f"/srv/{MARKER}/catalog.json"}}
        await connection.send(json.dumps({"id": message["id"], "result": result}))


async def test_the_inner_proof_does_not_follow_a_proxy(proxied):
    async with serve(app_server, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        assert await front.genuine_inner(url, INNER, MARKER) is True
    assert proxied.connections == 0


async def test_the_relay_and_the_launcher_health_check_do_not_follow_a_proxy(
    proxied, tmp_path, monkeypatch
):
    async with serve(app_server, "127.0.0.1", 0) as inner:
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
            ) as chat:
                opening = {"id": 1, "method": "initialize", "params": policy.initialize_params()}
                await chat.send(json.dumps(opening))
                reply = json.loads(await asyncio.wait_for(chat.recv(), 10))
                assert reply == {"id": 1, "result": {}}
            monkeypatch.setattr(launcher, "PORT", port)
            assert await launcher.healthy(SECRET, "127.0.0.1") is True
    assert proxied.connections == 0


def test_the_application_dials_the_bridge_without_a_proxy(proxied, monkeypatch):
    """The whole path from the browser, against a listener that is not the bridge."""
    received: list = []
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def listener():
        async def handler(connection):
            received.append(connection.request.headers.get("Authorization"))

        async def main():
            async with serve(handler, "127.0.0.1", port):
                await asyncio.sleep(30)

        asyncio.run(main())

    threading.Thread(target=listener, daemon=True).start()
    application = build(monkeypatch, app_public_url=BASE, codex_bridge_url=f"ws://127.0.0.1:{port}")
    application.state.settings.codex_bridge_token = SECRET
    http = TestClient(application, base_url=BASE)
    cookie = http.post("/login", data={"token": TOKEN}, follow_redirects=False).headers[
        "set-cookie"
    ]
    with http.websocket_connect(
        "ws://localhost:8000/api/codex/chat",
        headers={"cookie": cookie.split(";")[0], "origin": BASE},
    ) as chat:
        assert chat.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            chat.receive_json()
    assert proxied.connections == 0
    assert received == []


def websocket_connects(path):
    """Calls of ``websockets``' ``connect`` in a file that imports it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("websockets")
        and any(alias.name == "connect" for alias in node.names)
        for node in ast.walk(tree)
    )
    if not imported:
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "connect"
    ]


def test_every_websocket_dial_in_the_product_sets_proxy_none():
    """A new dial cannot silently inherit the proxy: the one rule every site has to follow."""
    sites = []
    for directory in ("app", "scripts"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            sites += [(path, call) for call in websocket_connects(path)]
    assert len(sites) >= 4
    missing = [
        f"{path.relative_to(ROOT)}:{call.lineno}"
        for path, call in sites
        if not any(
            keyword.arg == "proxy"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is None
            for keyword in call.keywords
        )
    ]
    assert missing == []
