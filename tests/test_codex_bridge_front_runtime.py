"""The bridge front in front of the real Codex CLI: no command, no other thread, a working chat.

Like tests/test_codex_bridge_runtime.py this runs the actual CLI in throw-away HOME/CODEX_HOME
directories with a synthetic loopback provider: no account, no real model, no user data. It runs
only when HEALTHOS_CODEX_CLI names a CLI binary (absolute path).
"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.services.umay import chat_relay, codex_bridge_policy, codex_bridge_tls
from app.services.umay.chat_socket import ChatDisconnected
from app.services.umay.codex_chat import CodexRPC
from app.services.umay.codex_chat_thread import chat_thread_params
from app.services.umay.codex_runtime_probe import probe_config, read_runtime
from app.services.umay.codex_runtime_rpc import (
    SyntheticResponses,
    exec_call,
    isolated_env,
    runtime_env,
)

CLI = os.environ.get("HEALTHOS_CODEX_CLI", "").strip()
pytestmark = pytest.mark.skipif(
    not (CLI and Path(CLI).is_absolute() and Path(CLI).is_file()),
    reason="real Codex CLI tests need HEALTHOS_CODEX_CLI=<absolute path to the codex binary>",
)
FRONT = Path(__file__).parents[1] / "scripts" / "codex_bridge_front.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RealBridge:
    def __init__(self, root: Path) -> None:
        self.provider = SyntheticResponses()
        self.token = secrets.token_urlsafe(48)
        self.inner_token = secrets.token_urlsafe(48)
        self.port, self.inner_port = free_port(), free_port()
        self.outside = root / "outside"
        self.home = root / "bridge"
        self.root = root
        for directory in (self.outside, self.home, self.home / "workspace", root / "h", root / "t"):
            directory.mkdir(parents=True)
        (root / "info").mkdir()

    def start(self) -> subprocess.Popen[bytes]:
        info = self.root / "info"
        _, bundled = asyncio.run(read_runtime([CLI], isolated_env(info, info, info), info))
        marker = "verified-" + secrets.token_hex(12)
        (self.home / marker).mkdir()
        catalog = self.home / marker / codex_bridge_policy.catalog_file_name()
        catalog.write_text(json.dumps(codex_bridge_policy.bridge_catalog(bundled)))
        config = codex_bridge_policy.bridge_config(str(catalog))
        (self.home / "config.toml").write_text(probe_config(config, self.provider.port, True))
        (self.home / "bridge.token").write_text(self.token)
        (self.home / "inner.token").write_text(self.inner_token)
        command = [
            CLI,
            "app-server",
            "--listen",
            f"ws://127.0.0.1:{self.inner_port}",
            "--ws-auth",
            "capability-token",
            "--ws-token-file",
            str(self.home / "inner.token"),
        ]
        settings = {
            "home": str(self.home),
            "host": "127.0.0.1",
            "port": self.port,
            "inner_port": self.inner_port,
            "marker": marker,
            "command": command,
        }
        (self.home / "front.json").write_text(json.dumps(settings))
        env = runtime_env(self.root / "h", self.home, self.root / "t")
        return subprocess.Popen(
            [sys.executable, str(FRONT), str(self.home / "front.json")],
            cwd=self.home / "workspace",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def dial(self):
        return connect(
            f"wss://127.0.0.1:{self.port}",
            ssl=codex_bridge_tls.client_context(self.token),
            additional_headers={"Authorization": f"Bearer {self.token}"},
            open_timeout=15,
            close_timeout=2,
            max_size=4 * 1024 * 1024,
        )

    def direct(self):
        return connect(
            f"ws://127.0.0.1:{self.inner_port}",
            additional_headers={"Authorization": f"Bearer {self.inner_token}"},
            open_timeout=15,
            close_timeout=2,
        )


@pytest.fixture
def real_bridge(tmp_path, local_sockets):
    bridge = RealBridge(tmp_path)
    process = bridge.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and process.poll() is None:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", bridge.port)) == 0:
                    break
            time.sleep(0.1)  # poll interval of the bounded wait above
        else:
            pytest.fail("the front did not start")
        yield bridge
    finally:
        process.terminate()
        process.wait(timeout=20)
        bridge.provider.close()


async def initialize(socket_) -> None:
    rpc = CodexRPC(socket_)
    await rpc.call("initialize", codex_bridge_policy.initialize_params())
    await rpc.send({"method": "initialized", "params": {}})


def write_sentinel(path: Path) -> list[str]:
    return [sys.executable, "-c", f"open({str(path)!r}, 'w').write('X')"]


def test_command_exec_runs_only_behind_the_inner_token(real_bridge):
    """Positive control: the very request that is refused at the front does run on the CLI."""
    sentinel = real_bridge.outside / "sentinel"
    params = {
        "command": write_sentinel(sentinel),
        "cwd": str(real_bridge.root),
        "sandboxPolicy": {"type": "dangerFullAccess"},
        "timeoutMs": 20000,
    }

    async def scenario():
        async with real_bridge.dial() as bridge:
            await initialize(bridge)
            await bridge.send(json.dumps({"id": 9, "method": "command/exec", "params": params}))
            with pytest.raises(ConnectionClosed) as ended:
                while True:
                    reply = json.loads(await asyncio.wait_for(bridge.recv(), 20))
                    assert "result" not in reply
            assert ended.value.rcvd.code == 1008
        assert not sentinel.exists()
        async with real_bridge.direct() as inner:
            rpc = CodexRPC(inner)
            await initialize(inner)
            assert (await rpc.call("command/exec", params))["exitCode"] == 0
        assert sentinel.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "config/value/write",
            {"keyPath": "features.shell_tool", "value": True, "mergeStrategy": "replace"},
        ),
        (
            "thread/start",
            {**chat_thread_params(), "sandbox": "danger-full-access"},
        ),
    ],
)
def test_other_requests_end_at_the_front(real_bridge, method, params):
    before = (real_bridge.home / "config.toml").read_text()

    async def scenario():
        async with real_bridge.dial() as bridge:
            await initialize(bridge)
            await bridge.send(json.dumps({"id": 9, "method": method, "params": params}))
            with pytest.raises(ConnectionClosed):
                while True:
                    assert "result" not in json.loads(await asyncio.wait_for(bridge.recv(), 20))

    asyncio.run(scenario())
    assert (real_bridge.home / "config.toml").read_text() == before


class Browser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def receive_json(self):
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, item):
        await self.outgoing.put(item)

    async def close(self, code=1000):
        pass


def test_the_chat_runs_a_turn_with_a_health_tool_call_through_the_front(real_bridge, monkeypatch):
    calls = []

    async def synthetic_archive(_sessions, arguments):
        calls.append(arguments)
        return {"synthetic_archive": "front-ok"}

    monkeypatch.setattr(chat_relay, "read_health", synthetic_archive)
    provider = real_bridge.provider

    async def scenario():
        async with real_bridge.dial() as bridge:
            rpc = CodexRPC(bridge)
            await initialize(bridge)
            # The synthetic provider has no ChatGPT account; production requires one here.
            assert (await rpc.call("account/read", {}))["account"] is None
            config = await rpc.call("config/read", {})
            assert codex_bridge_policy.bridge_problem(config) is None
            started = await rpc.call("thread/start", chat_thread_params())
            assert codex_bridge_policy.thread_problem(started) is None
            source = 'text(await tools.health_archive({action: "inventory"}));'
            with provider.lock:
                provider.queue[:] = [[exec_call("c_front", source)]]
            browser = Browser()
            relay = asyncio.create_task(chat_relay.relay(browser, rpc, started["thread"]["id"]))
            await browser.incoming.put({"text": "Sentetik soru", "timezone": "UTC"})
            events = []
            async with asyncio.timeout(60):
                while not events or events[-1].get("type") != "done":
                    events.append(await browser.outgoing.get())
            await browser.incoming.put(ChatDisconnected())
            await asyncio.gather(relay, return_exceptions=True)
            return events

    events = asyncio.run(scenario())
    assert events[-1]["status"] == "completed"
    assert calls == [{"action": "inventory"}]
    assert "front-ok" in json.dumps(provider.requests[-1])
