"""The application proves the bridge's identity before any bearer or message leaves.

An impostor holding the bridge address (the finding this closes) never gets past TLS: real
sockets, a real front process in front of a fake app-server. The real Codex CLI is not run here.
"""

import asyncio
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from support.auth import BASE, TOKEN, build
from support.codex_bridge import FAKE_APP_SERVER, LAUNCHER_MAIN, LAUNCHER_UNDER_TEST
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from app.services.umay import codex_bridge_tls as tls
from app.services.umay.codex_bridge_policy import catalog_file_name, initialize_params
from scripts import codex_bridge_front as front
from scripts import start_codex_bridge as launcher

SECRET = "bridge-secret-" + "k3y-" * 12
pytestmark = pytest.mark.usefixtures("local_sockets")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_certificate_is_reproducible_and_bound_to_the_token():
    assert tls.certificate_pem(SECRET) == tls.certificate_pem(SECRET)
    assert tls.certificate_pem(SECRET) != tls.certificate_pem("another-" + SECRET)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("ws://host.docker.internal:8766", "wss://host.docker.internal:8766"),
        ("wss://127.0.0.1:8766", "wss://127.0.0.1:8766"),
    ],
)
def test_a_url_from_before_this_change_is_still_dialled_over_tls(url, expected):
    assert tls.secure_bridge_url(url) == expected


async def _impostor_conversation(port: int) -> None:
    """The application's dial to an impostor: it never reaches the protocol."""
    with pytest.raises((ssl.SSLError, ConnectionResetError)):
        async with connect(
            f"wss://127.0.0.1:{port}",
            ssl=tls.client_context(SECRET),
            additional_headers={"Authorization": f"Bearer {SECRET}"},
            open_timeout=5,
        ):
            pytest.fail("the connection must not open")


async def test_plain_listener_gets_neither_token_nor_handshake():
    received: list = []

    async def handler(connection):
        received.append(connection.request.headers.get("Authorization"))
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        await _impostor_conversation(server.sockets[0].getsockname()[1])
    assert received == []


async def test_tls_listener_with_any_other_key_gets_neither_token_nor_handshake(tmp_path):
    received: list = []

    async def handler(connection):
        received.append(connection.request.headers.get("Authorization"))

    context = tls.server_context("an-impostor-who-does-not-hold-the-token-" * 2, tmp_path)
    async with serve(handler, "127.0.0.1", 0, ssl=context) as server:
        await _impostor_conversation(server.sockets[0].getsockname()[1])
    assert received == []


def test_chat_with_an_impostor_tells_the_user_to_restart_the_bridge(monkeypatch):
    """The whole path from the browser: an impostor sees no bearer and no question."""
    received: list = []
    port = free_port()

    def impostor():
        async def handler(connection):
            received.append(connection.request.headers.get("Authorization"))

        async def main():
            async with serve(handler, "127.0.0.1", port):
                await asyncio.sleep(30)

        asyncio.run(main())

    threading.Thread(target=impostor, daemon=True).start()
    time.sleep(0.3)  # the impostor is listening before the chat dials
    application = build(monkeypatch, app_public_url=BASE, codex_bridge_url=f"ws://127.0.0.1:{port}")
    application.state.settings.codex_bridge_token = SECRET
    http = TestClient(application, base_url=BASE)
    response = http.post("/login", data={"token": TOKEN}, follow_redirects=False)
    cookie = response.headers["set-cookie"].split(";")[0]
    with http.websocket_connect(
        "ws://localhost:8000/api/codex/chat", headers={"cookie": cookie, "origin": BASE}
    ) as chat:
        message = chat.receive_json()
        assert message["type"] == "error" and "köprüsünü yeniden" in message["text"]
        assert "codex" not in message["text"].lower()
        with pytest.raises(WebSocketDisconnect):
            chat.receive_json()
    assert received == []


async def fake_inner(token: str, marker: str):
    """A listener speaking the app-server protocol, reporting ``marker`` from config/read."""

    async def handler(connection):
        assert connection.request.headers.get("Authorization") == f"Bearer {token}"
        async for raw in connection:
            message = json.loads(raw)
            if "id" not in message:
                continue
            result = {}
            if message["method"] == "config/read":
                catalog = f"/srv/{marker}/{catalog_file_name()}"
                result = {"config": {"model_catalog_json": catalog}}
            await connection.send(json.dumps({"id": message["id"], "result": result}))

    return await serve(handler, "127.0.0.1", 0)


async def test_only_the_configured_listener_passes_the_inner_proof():
    inner_token = "inner-" + "t0k3n" * 10
    async with await fake_inner(inner_token, "verified-abc") as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        assert await front.genuine_inner(url, inner_token, "verified-abc") is True
    # A process that took the port does not know the launcher's random directory.
    async with await fake_inner(inner_token, "public-guess") as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        assert await front.genuine_inner(url, inner_token, "verified-abc") is False


async def test_inner_proof_is_false_when_nothing_listens():
    assert await front.genuine_inner(f"ws://127.0.0.1:{free_port()}", "x", "y") is False


@pytest.mark.parametrize(
    ("values", "allowed"),
    [
        (["Bearer good"], True),
        (["bearer good"], True),
        (["Bearer wrong"], False),
        ([], False),
        (["Basic good"], False),
        (["good"], False),
        (["Bearer good", "Bearer x"], False),
        (["Bearer g\udcc3ood"], False),
    ],
)
def test_only_one_bearer_of_the_bridge_token_is_authorized(values, allowed):
    assert front.authorized(values, "good") is allowed


@pytest.fixture
def running_front(tmp_path):
    """The launcher, the real front and a fake app-server on a private port."""
    script = tmp_path / "scripts" / "start_codex_bridge.py"
    script.parent.mkdir()
    script.write_text(Path(launcher.__file__).read_text())
    front_path = Path(launcher.__file__).with_name("codex_bridge_front.py")
    (tmp_path / "scripts" / front_path.name).write_text(front_path.read_text())
    (tmp_path / "app").symlink_to(Path(launcher.__file__).parents[1] / "app")
    cli = tmp_path / "codex"
    cli.write_text(
        f"#!{sys.executable}\n"
        + FAKE_APP_SERVER
        + "import os, sys, time\nfrom pathlib import Path\n"
        + 'serve_app_server(sys.argv, Path(os.environ["CODEX_HOME"]))\n'
        + "time.sleep(300)\n"
    )
    cli.chmod(0o700)
    source = tmp_path / "identity"
    state = source / "healthos-bridge"
    state.mkdir(parents=True)
    (source / "auth.json").write_text("{}")
    (state / "bridge.token").write_text(SECRET)
    port = free_port()
    environment = dict(
        os.environ,
        CODEX_HOME=str(source),
        CODEX_BRIDGE_BIND_HOST="127.0.0.1",
        BRIDGE_TEST_PORT=str(port),
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
    )
    environment.pop("CODEX_BRIDGE_TOKEN", None)
    verified = (
        LAUNCHER_UNDER_TEST
        + "import json\n"
        + "def verified(command, executable, home):\n"
        + '    return {"catalog_text": json.dumps({"models": []}),\n'
        + '            "runtime": {"cli_sha256": launcher["file_digest"](executable)}}\n'
        + 'launcher["verify_runtime"] = verified\n'
        + LAUNCHER_MAIN
    )
    process = subprocess.Popen(
        [sys.executable, "-c", verified, str(script), "--foreground"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline and process.poll() is None:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)  # poll interval of the bounded wait above
        else:
            pytest.fail("the front did not start: " + process.stderr.read().decode())
        yield port, state
    finally:
        process.terminate()
        process.communicate(timeout=30)


async def test_genuine_front_opens_the_chat_and_never_forwards_the_bridge_token(
    running_front, monkeypatch
):
    port, state = running_front
    async with connect(
        f"wss://127.0.0.1:{port}",
        ssl=tls.client_context(SECRET),
        additional_headers={"Authorization": f"Bearer {SECRET}"},
        open_timeout=10,
    ) as bridge:
        opening = {"id": 1, "method": "initialize", "params": initialize_params()}
        await bridge.send(json.dumps(opening))
        assert json.loads(await asyncio.wait_for(bridge.recv(), 10)) == {"id": 1, "result": {}}
    assert (state / "inner.token").read_text() != SECRET
    # The launcher's own health check speaks the same pinned TLS.
    monkeypatch.setattr(launcher, "PORT", port)
    assert await launcher.healthy(SECRET, "127.0.0.1") is True
    assert await launcher.healthy("not-the-bridge-token-" + SECRET, "127.0.0.1") is False


@pytest.mark.parametrize("bearer", [None, "the-wrong-token"])
async def test_front_refuses_a_caller_without_the_bridge_bearer(running_front, bearer):
    port, _ = running_front
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    with pytest.raises(Exception, match="401"):
        async with connect(
            f"wss://127.0.0.1:{port}", ssl=tls.client_context(SECRET), additional_headers=headers
        ):
            pytest.fail("the connection must not open")


def test_tls_key_material_does_not_stay_on_disk(tmp_path):
    tls.server_context(SECRET, tmp_path)
    assert list(tmp_path.iterdir()) == []
