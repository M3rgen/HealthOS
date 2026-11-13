"""The launcher fails closed: an unknown or misbehaving runtime never serves Umay.

Synthetic CLIs exercise the real verification path (no monkeypatched verification): the
launcher must exit with an error before copying the login or starting the app-server, and
record why without secrets. Real-CLI behaviour is covered by tests/test_codex_bridge_runtime.py.
"""

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from support.codex_bridge import (
    LAUNCHER_MAIN,
    LAUNCHER_UNDER_TEST,
    ScriptedBridge,
    attested_thread,
    bridge_replies,
)

from app.services.umay import codex_bridge_policy as policy
from app.services.umay import codex_runtime_probe as probe
from scripts import start_codex_bridge as bridge

FAKE_CLI = """\
import json, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli VERSION")
elif args[:3] == ["debug", "models", "--bundled"]:
    print(json.dumps({"models": [{"slug": "gpt-6.1-sol", "multi_agent_version": "v2"}]}))
elif args[:1] == ["app-server"]:
    # A runtime that dies (or listens) instead of answering the probe protocol.
    Path(sys.argv[0]).with_name("app-server-started").write_text(" ".join(args))
    sys.exit(3)
"""


def run_launcher(tmp_path, version, **env_overrides):
    cli = tmp_path / "codex"
    cli.write_text(f"#!{sys.executable}\n" + FAKE_CLI.replace("VERSION", version))
    cli.chmod(0o700)
    source = tmp_path / "identity"
    state = source / "healthos-bridge"
    state.mkdir(parents=True)
    (source / "auth.json").write_text('{"synthetic": "login"}')
    token = secrets.token_urlsafe(32)
    (state / "bridge.token").write_text(token)
    env = dict(
        os.environ,
        CODEX_HOME=str(source),
        CODEX_BRIDGE_BIND_HOST="127.0.0.1",
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
    )
    env.pop("CODEX_BRIDGE_TOKEN", None)
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", LAUNCHER_UNDER_TEST + LAUNCHER_MAIN]
        + [str(Path(bridge.__file__)), "--foreground"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert token not in result.stdout + result.stderr
    return result, state, cli


@pytest.mark.parametrize("version", ["0.0.1", "0.161.0-alpha"])
def test_unknown_runtime_version_never_starts_the_bridge(tmp_path, version):
    result, state, cli = run_launcher(tmp_path, version)
    assert result.returncode != 0
    assert "güvenlik doğrulamasını geçmedi" in result.stderr
    assert f"{version} is not a verified bridge runtime" in result.stderr
    assert not cli.with_name("app-server-started").exists()
    assert not (state / "auth.json").exists() and not (state / "config.toml").exists()
    record = json.loads((state / bridge.ATTESTATION).read_text())
    assert record["passed"] is False and record["policy_sha256"] == bridge.policy_fingerprint()
    assert (state / bridge.ATTESTATION).stat().st_mode & 0o777 == 0o600
    assert list((state / "probe").iterdir()) == []


def test_runtime_that_fails_the_probe_never_starts_the_bridge(tmp_path, local_sockets):
    (version,) = policy.SUPPORTED_CLI_VERSIONS
    result, state, cli = run_launcher(tmp_path, version)
    assert result.returncode != 0
    assert "güvenlik doğrulamasını geçmedi" in result.stderr
    # Only the isolated probe ran the fake app-server (stdio), never the bridge listener.
    assert cli.with_name("app-server-started").read_text() == "app-server --listen stdio://"
    assert not (state / "auth.json").exists() and not (state / "config.toml").exists()
    assert json.loads((state / bridge.ATTESTATION).read_text())["passed"] is False


def test_configured_cli_must_be_an_absolute_file(tmp_path):
    result, state, _ = run_launcher(tmp_path, "0.161.0", CODEX_BRIDGE_CLI="codex")
    assert result.returncode != 0 and "CODEX_BRIDGE_CLI" in result.stderr
    assert not (state / "auth.json").exists()


def test_running_bridge_without_current_attestation_is_not_reused(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "identity"))
    monkeypatch.setenv("CODEX_BRIDGE_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "synthetic-bridge-token-0123456789-abcdef")
    monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py"])
    monkeypatch.setattr(bridge, "healthy", AsyncMock(return_value=True))
    state = tmp_path / "identity" / "healthos-bridge"
    for record in (None, {"passed": True, "policy_sha256": "older-policy"}, {"passed": False}):
        if record is not None:
            state.mkdir(parents=True, exist_ok=True)
            (state / bridge.ATTESTATION).write_text(json.dumps(record))
        with pytest.raises(SystemExit, match="güncel güvenlik doğrulamasıyla"):
            bridge.main()
    assert not (tmp_path / ".env").exists()


def test_bridge_config_and_catalog_follow_the_policy(tmp_path):
    import tomllib

    config = tomllib.loads(policy.bridge_config(str(tmp_path / "model catalog.json")))
    assert config["model_catalog_json"] == str(tmp_path / "model catalog.json")
    assert config["model"] == policy.MODEL and config["web_search"] == "disabled"
    features = config["features"]
    for feature in ("view_image", "multi_agent", "shell_tool", "apps", "plugins", "hooks"):
        assert features[feature] is False, feature
    assert features["multi_agent_v2"] == {
        "enabled": False,
        "max_concurrent_threads_per_session": 1,
    }
    bundled = {
        "models": [
            {"slug": "other", "multi_agent_version": "v2"},
            {
                "slug": policy.MODEL,
                "multi_agent_version": "v2",
                "apply_patch_tool_type": "freeform",
                "tool_mode": "code_mode_only",
            },
        ]
    }
    assert policy.bridge_catalog(bundled) == {
        "models": [{"slug": policy.MODEL, "tool_mode": "code_mode_only"}]
    }
    with pytest.raises(ValueError):
        policy.bridge_catalog({"models": [{"slug": "other"}]})
    assert policy.thread_config()["features.view_image"] is False
    params = policy.thread_start_params("base", "persona", [{"name": "health_archive"}])
    assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({}, None),
        ({"thread": {"id": "t", "cliVersion": "0.162.0"}}, "unsupported runtime version"),
        ({"thread": {"id": "t"}}, "unsupported runtime version"),
        ({"model": "other"}, "unexpected model"),
        ({"approvalPolicy": "on-request"}, "unexpected approval policy"),
        ({"sandbox": {"type": "readOnly", "networkAccess": True}}, "unexpected sandbox"),
        ({"sandbox": {"type": "workspaceWrite", "networkAccess": False}}, "unexpected sandbox"),
    ],
)
def test_thread_problem(change, problem):
    result = {
        "thread": {"id": "t", "cliVersion": "0.161.0"},
        "model": policy.MODEL,
        "approvalPolicy": "never",
        "sandbox": {"type": "readOnly", "networkAccess": False},
    }
    assert policy.thread_problem(result | change) == problem


async def test_chat_refuses_an_unattested_thread_before_any_user_text():
    from app.services.umay.codex_chat import BridgeUnverified, CodexRPC

    thread = attested_thread("t")
    thread["thread"]["cliVersion"] = "9.9.9"
    connection = ScriptedBridge(bridge_replies(thread_start=thread))
    with pytest.raises(BridgeUnverified, match="unsupported runtime version"):
        await CodexRPC(connection).start()
    assert connection.methods()[-1] == "thread/start"
    assert "turn/start" not in connection.methods()


def test_probe_helpers_measure_catalog_children_and_images():
    image = probe.synthetic_png()
    assert probe.decode_png(image) == b"\0\xff\0\0\xff\0\0\0\0\xff\0\0\xff\0"
    body = {
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "tools": [
                            {
                                "type": "custom",
                                "name": "exec",
                                "description": "x\n### `health_archive`\ny\n### `view_image`\n",
                            }
                        ],
                    },
                    {
                        "type": "namespace",
                        "name": "collaboration",
                        "tools": [{"name": "spawn_agent"}],
                    },
                ],
            }
        ]
    }
    assert probe.catalog(body) == (
        {"functions.exec", "collaboration.spawn_agent"},
        {"health_archive", "view_image"},
    )
    child = {"input": [{"type": "agent_message", "content": [{"type": "input_text"}]}]}
    parent = {"input": [{"type": "function_call", "arguments": probe.CHILD_MARKER}]}
    assert probe.child_prompt(child) and not probe.child_prompt(parent)
    import base64

    leaked = json.dumps({"image_url": "data:image/png;base64," + base64.b64encode(image).decode()})
    assert probe.carries_image(leaked, image)
    assert not probe.carries_image('{"output": "unsupported call: view_image"}', image)
