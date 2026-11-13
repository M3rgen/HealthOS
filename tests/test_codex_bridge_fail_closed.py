"""The chat fails closed against a bridge it cannot tie to the current policy (R1 O1).

A bridge left running from an older launcher, or started by hand, passes the thread checks
(CLI version, model, approval, sandbox) yet offers sub-agents. Before any thread exists the
chat compares the bridge's effective configuration (``config/read``) with the policy, and
every thread repeats all limits. Real-CLI counterparts: tests/test_codex_bridge_runtime.py.
"""

import json
import sys
import tomllib

import pytest
from support.codex_bridge import ScriptedBridge, bridge_replies, verified_bridge_config

from app.services.umay import codex_bridge_policy as policy
from app.services.umay.codex_chat import BridgeUnverified, CodexRPC
from scripts import start_codex_bridge as bridge

# ``config/read`` of the real CLI 0.161.0 serving the pre-BRIDGE-01/02 config.toml (measured
# 8 November 2026): no catalog, no image or agent limit.
OLD_BRIDGE_CONFIG = {
    "config": {
        "model": "gpt-6.1-sol",
        "web_search": "disabled",
        "model_catalog_json": None,
        "features": {
            "code_mode": False,
            "apps": False,
            "browser_use": False,
            "computer_use": False,
            "hooks": False,
            "image_generation": False,
            "multi_agent": False,
            "plugins": False,
            "shell_tool": False,
            "unified_exec": False,
            "memories": False,
        },
    },
    "origins": {},
}


def test_thread_config_repeats_every_limit_of_the_bridge_config():
    config = policy.thread_config()
    file_features = tomllib.loads(policy.bridge_config("/x/catalog.json"))["features"]
    agents = file_features.pop("multi_agent_v2")
    for feature, value in file_features.items():
        assert config[f"features.{feature}"] is value, feature
    for key, value in agents.items():
        assert config[f"features.multi_agent_v2.{key}"] == value, key
    assert {key.split(".")[1] for key in config if key.startswith("features.")} == {
        *policy.DISABLED_FEATURES,
        "multi_agent_v2",
    }
    assert config["web_search"] == "disabled"
    assert policy.thread_start_params("b", "d", [])["config"] == config


def config_with(**changes):
    result = verified_bridge_config()
    config = result["config"]
    for key, value in changes.items():
        if key in ("enabled", "max_concurrent_threads_per_session"):
            config["features"]["multi_agent_v2"][key] = value
        elif key in policy.DISABLED_FEATURES or key == "multi_agent_v2":
            config["features"][key] = value
        else:
            config[key] = value
    return result


@pytest.mark.parametrize(
    ("result", "problem"),
    [
        (verified_bridge_config(), None),
        (verified_bridge_config("C:\\Users\\bridge\\.codex\\healthos-bridge"), None),
        (OLD_BRIDGE_CONFIG, "bridge not verified under the current policy"),
        ({}, "unexpected bridge model settings"),
        (config_with(model="other"), "unexpected bridge model settings"),
        (config_with(web_search="live"), "unexpected bridge model settings"),
        (config_with(model_catalog_json=None), "bridge not verified under the current policy"),
        (
            config_with(model_catalog_json="/srv/bridge/model-catalog.json"),
            "bridge not verified under the current policy",
        ),
        (
            config_with(model_catalog_json=f"/srv/{policy.catalog_file_name('0' * 64)}"),
            "bridge not verified under the current policy",
        ),
        (config_with(view_image=True), "bridge feature not disabled"),
        (config_with(view_image=None), "bridge feature not disabled"),
        (config_with(multi_agent_v2=None), "bridge agent limit missing"),
        (config_with(enabled=True), "bridge agent limit missing"),
        (config_with(max_concurrent_threads_per_session=2), "bridge agent limit missing"),
    ],
)
def test_bridge_problem(result, problem):
    assert policy.bridge_problem(result) == problem


async def test_new_chat_with_an_old_bridge_is_refused_before_any_thread_or_user_text():
    connection = ScriptedBridge(bridge_replies(config_read=OLD_BRIDGE_CONFIG))
    with pytest.raises(BridgeUnverified, match="not verified under the current policy"):
        await CodexRPC(connection).start()
    # No thread, so no turn, no user text and nothing that could start a sub-agent.
    assert connection.methods() == ["initialize", "initialized", "account/read", "config/read"]


async def test_bridge_without_config_read_is_refused():
    replies = bridge_replies()
    del replies["config/read"]
    connection = ScriptedBridge(replies)
    with pytest.raises(RuntimeError, match="unknown method config/read"):
        await CodexRPC(connection).start()
    assert "thread/start" not in connection.methods()


async def test_verified_bridge_opens_a_thread_with_every_limit():
    connection = ScriptedBridge(bridge_replies())
    assert await CodexRPC(connection).start() == ("t", policy.MODEL)
    assert connection.methods()[-2:] == ["config/read", "thread/start"]
    assert connection.sent[-1]["params"]["config"] == policy.thread_config()


def test_policy_fingerprint_covers_catalog_tools_and_probe(monkeypatch):
    base = policy.policy_fingerprint()
    assert policy.catalog_file_name() == f"model-catalog-{base}.json"
    assert bridge.policy_fingerprint() == base
    changes = {
        "CATALOG_REMOVED_KEYS": (*policy.CATALOG_REMOVED_KEYS, "tool_mode"),
        "REQUIRED_NESTED_TOOLS": frozenset(),
        "ALLOWED_TOOLS": policy.ALLOWED_TOOLS | {"functions.view_image"},
        "SUPPORTED_CLI_VERSIONS": frozenset({"0.161.0", "0.162.0"}),
        "DISABLED_FEATURES": policy.DISABLED_FEATURES[1:],
        "PROBE_SOURCES": policy.PROBE_SOURCES[:-1],
    }
    for name, value in changes.items():
        with monkeypatch.context() as patch:
            patch.setattr(policy, name, value)
            assert policy.policy_fingerprint() != base, name


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """The manual launcher in process, with a synthetic CODEX_HOME and no real CLI."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "identity"))
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "synthetic-bridge-token-0123456789-abcdef")
    monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py"])
    state = tmp_path / "identity" / "healthos-bridge"
    state.mkdir(parents=True)

    def never(*_args):
        raise AssertionError("the runtime must not be selected or measured")

    monkeypatch.setattr(bridge, "select_cli", never)
    monkeypatch.setattr(bridge, "verify_runtime", never)
    return state, lambda healthy, listening: (
        monkeypatch.setattr(bridge, "healthy", AsyncMock(return_value=healthy)),
        monkeypatch.setattr(bridge, "listening", lambda _host: listening),
    )


def test_unverified_running_bridge_is_reported_with_its_pid(launcher, tmp_path):
    state, running = launcher
    running(healthy=True, listening=True)
    (state / "bridge.pid").write_text("4242")
    (state / bridge.ATTESTATION).write_text(json.dumps({"passed": True, "policy_sha256": "old"}))
    with pytest.raises(SystemExit) as stopped:
        bridge.main()
    message = str(stopped.value.code)
    assert "Umay bu köprüyle sohbet açmaz" in message and "PID 4242" in message
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("foreground", [False, True])
def test_occupied_bridge_address_is_reported_before_the_probe(
    launcher, tmp_path, monkeypatch, foreground
):
    """Another token or the service next to a manual bridge: a clear error, not a bind
    failure while the old process keeps serving."""
    state, running = launcher
    running(healthy=False, listening=True)
    if foreground:
        monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py", "--foreground"])
    with pytest.raises(SystemExit) as stopped:
        bridge.main()
    message = str(stopped.value.code)
    assert "zaten bir süreç dinliyor" in message and "sohbet açmaz" in message
    assert not (state / "auth.json").exists() and not (state / "config.toml").exists()


def test_listening_detects_an_occupied_address(monkeypatch, local_sockets):
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        monkeypatch.setattr(bridge, "PORT", server.getsockname()[1])
        assert bridge.listening("127.0.0.1")
    assert not bridge.listening("127.0.0.1")
