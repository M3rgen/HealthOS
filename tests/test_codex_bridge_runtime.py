"""Real Codex CLI regression for the Umay bridge boundaries (BRIDGE-01, BRIDGE-02).

These tests start the actual CLI app-server in throw-away HOME/CODEX_HOME directories with a
synthetic loopback Responses provider: no account, no real model, no user data. They run only
when HEALTHOS_CODEX_CLI names a CLI binary (absolute path); without it they are skipped with
that reason, so CI and machines without the CLI stay green. Run before adding a release to
codex_bridge_policy.SUPPORTED_CLI_VERSIONS:

    HEALTHOS_CODEX_CLI=/path/to/codex uv run --frozen --extra dev pytest -q \\
        tests/test_codex_bridge_runtime.py
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
from websockets.exceptions import InvalidStatus

from app.services.umay import chat_relay as codex
from app.services.umay import codex_bridge_policy as policy
from app.services.umay.chat_socket import ChatDisconnected as WebSocketDisconnect
from app.services.umay.codex_chat import INSTRUCTIONS, BridgeUnverified, CodexRPC
from app.services.umay.codex_health import HEALTH_TOOL
from app.services.umay.codex_runtime_probe import (
    ProbeError,
    attest,
    probe_config,
    probe_runtime,
    probe_thread_params,
    read_runtime,
)
from app.services.umay.codex_runtime_rpc import (
    SyntheticResponses,
    call_outputs,
    exec_call,
    isolated_env,
    output_text,
    sha256_file,
)

CLI = os.environ.get("HEALTHOS_CODEX_CLI", "").strip()
pytestmark = pytest.mark.skipif(
    not (CLI and Path(CLI).is_absolute() and Path(CLI).is_file()),
    reason="real Codex CLI tests need HEALTHOS_CODEX_CLI=<absolute path to the codex binary>",
)

# The launcher's configuration and thread settings before BRIDGE-01/02 (7 November 2026).
PREVIOUS_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "code_mode",
    "computer_use",
    "browser_use",
    "image_generation",
    "hooks",
)
PREVIOUS_CONFIG = (
    'cli_auth_credentials_store = "file"\nweb_search = "disabled"\n'
    'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "medium"\n\n[features]\n'
    + "".join(f"{feature} = false\n" for feature in PREVIOUS_FEATURES)
)
PREVIOUS_THREAD = {
    "model_reasoning_effort": "high",
    "web_search": "disabled",
    "features.shell_tool": False,
    "features.multi_agent": False,
    "features.code_mode": False,
}


@pytest.fixture(scope="module")
def attestation(tmp_path_factory, module_local_sockets):
    work = tmp_path_factory.mktemp("bridge-probe")
    report = attest([CLI], Path(CLI), policy.bridge_config, work)
    assert list(work.iterdir()) == []  # the probe removes its own temporary files
    return report


def test_policy_runtime_offers_only_the_health_tool(attestation):
    assert attestation["passed"] and attestation["failures"] == []
    assert set(attestation["catalog"]) <= policy.ALLOWED_TOOLS
    assert set(attestation["nested_catalog"]) <= policy.ALLOWED_NESTED_TOOLS
    assert "health_archive" in attestation["nested_catalog"]
    nested = set(attestation["scenarios"]["runtime_nested_tools"])
    assert "health_archive" in nested and not nested & {"view_image", "apply_patch"}
    assert not any(name.startswith("collaboration.") for name in attestation["catalog"])
    runtime = attestation["runtime"]
    assert runtime["cli_version"] in policy.SUPPORTED_CLI_VERSIONS
    assert runtime["cli_sha256"] == sha256_file(Path(CLI))
    assert attestation["thread"]["sandbox"] == {"type": "readOnly", "networkAccess": False}
    assert attestation["bridge_config"] == "matches policy"


def test_bridge01_forced_image_reads_return_no_image(attestation):
    scenarios = attestation["scenarios"]
    names = [name for name in scenarios if "image_" in name and "skipped" not in scenarios[name]]
    assert {"image_absolute", "image_relative", "image_namespaced", "image_exec"} <= set(names)
    if sys.platform != "win32":
        assert "image_symlink" in names
    for name in names:
        result = scenarios[name]
        assert result["answered"] and not result["image_reached_model"], name
        assert result["model_requests"] == 2, name


def test_bridge02_forced_spawn_creates_no_child(attestation):
    scenarios = attestation["scenarios"]
    names = [name for name in scenarios if "spawn_" in name]
    assert len(names) == 8  # four call forms, with and without the bridge model catalog
    for name in names:
        result = scenarios[name]
        assert result["answered"], name
        assert result["child_model_requests"] == result["child_threads"] == 0, name
        assert result["model_requests"] == 2, name
        assert '"task_name":"/' not in result["result"], name
    # Without the bridge catalog the tool is offered again; the dispatch limit still refuses.
    assert "collaboration.spawn_agent" in attestation["fallback_catalog"]
    assert "limit reached" in scenarios["fallback_spawn_collaboration"]["result"]


def test_health_patch_and_sandbox_controls(attestation):
    scenarios = attestation["scenarios"]
    assert scenarios["health_archive"] == {"application_callbacks": 1, "result_delivered": True}
    assert scenarios["apply_patch"]["answered"] and scenarios["apply_patch"]["sentinel_unchanged"]
    sandbox = scenarios["sandbox"]
    assert sandbox["read_ok"] and sandbox["listener_reachable_unsandboxed"]
    assert sandbox["write_exit"] != 0 and sandbox["sentinel_unchanged"]
    assert sandbox["network_exit"] != 0 and not sandbox["network_connected"]


def test_probe_detects_the_previous_configuration(tmp_path, local_sockets):
    """Baseline: the old launcher settings leak the image and start a sub-agent."""
    report = asyncio.run(
        probe_runtime([CLI], PREVIOUS_CONFIG, "{}", tmp_path, probe_thread_params(PREVIOUS_THREAD))
    )
    failures = "\n".join(report["failures"])
    assert not report["passed"]
    assert "image_absolute: image content reached a model request" in failures
    assert "spawn_collaboration: a sub-agent was started" in failures
    assert report["scenarios"]["spawn_collaboration"]["child_model_requests"] >= 1
    assert "view_image" in report["nested_catalog"]
    # The positive control worked in the same run: failures are not a broken harness.
    assert report["scenarios"]["health_archive"]["result_delivered"]


def test_unknown_runtime_version_is_refused(tmp_path):
    shim = tmp_path / "codex-shim"
    shim.write_text(
        f'#!/bin/sh\n[ "$1" = --version ] && echo \'codex-cli 0.0.1\' && exit 0\nexec {CLI} "$@"\n'
    )
    shim.chmod(0o700)
    with pytest.raises(ProbeError, match="0.0.1 is not a verified bridge runtime"):
        attest([str(shim)], shim, policy.bridge_config, tmp_path / "work")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def bridges(tmp_path, local_sockets):
    """Starts the real CLI as a bridge (ws + capability token); ``config_for(catalog)`` gives
    its config.toml. Every bridge, provider and token file is removed afterwards."""
    started = []

    def start(config_for=policy.bridge_config, name="bridge"):
        base = tmp_path / name
        home, codex_home, workspace = base / "home", base / "codex-home", base / "ws"
        for path in (home, codex_home, workspace):
            path.mkdir(parents=True)
        env = isolated_env(home, codex_home, tmp_path)
        _, bundled = asyncio.run(read_runtime([CLI], env, tmp_path))
        catalog = codex_home / policy.catalog_file_name()
        catalog.write_text(json.dumps(policy.bridge_catalog(bundled)))
        provider = SyntheticResponses()
        config = probe_config(config_for(str(catalog)), provider.port, True)
        (codex_home / "config.toml").write_text(config)
        token = secrets.token_urlsafe(32)
        token_file = codex_home / "bridge.token"
        token_file.write_text(token)
        port = free_port()
        process = subprocess.Popen(
            [CLI, "app-server", "--listen", f"ws://127.0.0.1:{port}", "--ws-auth"]
            + ["capability-token", "--ws-token-file", str(token_file)],
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        started.append((process, provider, token_file))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as attempt:
                if attempt.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        return f"ws://127.0.0.1:{port}", token, provider

    try:
        yield start
    finally:
        for process, provider, token_file in started:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            provider.close()
            token_file.unlink()
            assert process.returncode is not None


@pytest.fixture
def ws_bridge(bridges):
    """The real CLI as the bridge with the bridge policy."""
    return bridges()


async def initialize(url, token):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with connect(url, additional_headers=headers) as ws:
        rpc = CodexRPC(ws)
        return await rpc.call("initialize", {"clientInfo": {"name": "t", "version": "1"}})


def test_websocket_token_is_required(ws_bridge):
    url, token, _ = ws_bridge
    for header in (None, "wrong-" + token):
        with pytest.raises(InvalidStatus) as refused:
            asyncio.run(initialize(url, header))
        assert refused.value.response.status_code == 401
    assert "userAgent" in asyncio.run(initialize(url, token))


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


def test_health_tool_runs_through_the_application_relay(ws_bridge, monkeypatch):
    """Real CLI -> HealthOS relay -> run_health_tool -> synthetic archive -> model request."""
    url, token, provider = ws_bridge
    calls = []

    async def synthetic_archive(_sessions, arguments):
        calls.append(arguments)
        return {"synthetic_archive": "relay-ok"}

    monkeypatch.setattr(codex, "read_health", synthetic_archive)

    async def scenario():
        headers = {"Authorization": f"Bearer {token}"}
        async with connect(url, additional_headers=headers, max_size=4 * 1024 * 1024) as ws:
            rpc = CodexRPC(ws)
            await rpc.call(
                "initialize",
                {
                    "clientInfo": {"name": "t", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await rpc.send({"method": "initialized", "params": {}})
            # The synthetic provider has no ChatGPT account; production keeps account/read.
            started = await rpc.call(
                "thread/start",
                policy.thread_start_params(INSTRUCTIONS, "Synthetic.", [HEALTH_TOOL]),
            )
            assert policy.thread_problem(started) is None
            source = 'text(await tools.health_archive({action: "inventory"}));'
            with provider.lock:
                provider.queue[:] = [[exec_call("c_relay", source)]]
            browser = Browser()
            relay = asyncio.create_task(codex.relay(browser, rpc, started["thread"]["id"]))
            await browser.incoming.put({"text": "Sentetik soru", "timezone": "UTC"})
            while (message := await asyncio.wait_for(browser.outgoing.get(), 60))["type"] != "done":
                pass
            await browser.incoming.put(WebSocketDisconnect())
            with pytest.raises(WebSocketDisconnect):
                await relay
            return message

    done = asyncio.run(scenario())
    assert done["status"] == "completed"
    assert calls == [{"action": "inventory"}]
    outputs = call_outputs(provider.requests, "c_relay")
    assert any("relay-ok" in output_text(output) for output in outputs)


class SyntheticAccountRPC(CodexRPC):
    """The chat's RPC with only ``account/read`` answered locally: the synthetic provider has
    no ChatGPT account (production keeps that check). Records every method it sends."""

    def __init__(self, connection):
        super().__init__(connection)
        self.methods = []

    async def call(self, method, params):
        if method == "account/read":
            self.methods.append(method)
            return {"account": {"type": "chatgpt"}}
        return await super().call(method, params)

    async def request(self, method, params):
        self.methods.append(method)
        return await super().request(method, params)


async def chat_start(url, token):
    headers = {"Authorization": f"Bearer {token}"}
    async with connect(url, additional_headers=headers, max_size=4 * 1024 * 1024) as ws:
        rpc = SyntheticAccountRPC(ws)
        try:
            return await rpc.start(), rpc.methods
        except BridgeUnverified as refused:
            return refused, rpc.methods


def test_new_chat_with_an_old_bridge_is_refused_before_user_text(bridges):
    """R1 O1: a bridge still running the pre-BRIDGE-01/02 configuration is refused before a
    thread exists; the policy bridge opens one (positive control in the same run)."""
    url, token, provider = bridges(lambda _catalog: PREVIOUS_CONFIG, "old")
    refused, methods = asyncio.run(chat_start(url, token))
    assert isinstance(refused, BridgeUnverified)
    assert "not verified under the current policy" in str(refused)
    assert methods == ["initialize", "account/read", "config/read"]
    assert provider.requests == []  # no model request: no user text, no sub-agent

    url, token, provider = bridges(name="current")
    (thread_id, model), methods = asyncio.run(chat_start(url, token))
    assert thread_id and model == policy.MODEL
    assert methods == ["initialize", "account/read", "config/read", "thread/start"]


def test_policy_bridge_configuration_is_recognised_by_the_cli(ws_bridge):
    """Every limit the chat checks and repeats per thread is a key of the CLI's own
    effective configuration (a renamed feature would read as missing and refuse)."""
    url, token, _ = ws_bridge

    async def read():
        headers = {"Authorization": f"Bearer {token}"}
        async with connect(url, additional_headers=headers, max_size=4 * 1024 * 1024) as ws:
            rpc = CodexRPC(ws)
            await rpc.call("initialize", {"clientInfo": {"name": "t", "version": "1"}})
            return await rpc.call("config/read", {})

    result = asyncio.run(read())
    assert policy.bridge_problem(result) is None
    features = result["config"]["features"]
    assert {feature: features.get(feature) for feature in policy.DISABLED_FEATURES} == {
        feature: False for feature in policy.DISABLED_FEATURES
    }
    assert features["multi_agent_v2"] == {
        "enabled": False,
        "max_concurrent_threads_per_session": 1,
    }


def test_new_thread_limits_stop_spawn_even_on_an_old_bridge(tmp_path, local_sockets):
    """Second layer of O1: the current thread settings on the old configuration (R1's
    experiment). The catalog still offers agents, so the bridge stays refused; a forced
    spawn is refused at dispatch."""
    report = asyncio.run(probe_runtime([CLI], PREVIOUS_CONFIG, "{}", tmp_path))
    assert not report["passed"]
    assert report["bridge_config"] == "bridge not verified under the current policy"
    assert "collaboration.spawn_agent" in report["catalog"]
    names = [name for name in report["scenarios"] if "spawn_" in name]
    assert len(names) == 8
    for name in names:
        result = report["scenarios"][name]
        assert result["answered"], name
        assert result["child_model_requests"] == result["child_threads"] == 0, name
        assert result["model_requests"] == 2, name
    assert "limit reached" in report["scenarios"]["spawn_collaboration"]["result"]
    assert not any("a sub-agent was started" in failure for failure in report["failures"])
