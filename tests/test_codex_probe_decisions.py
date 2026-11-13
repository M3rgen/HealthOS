"""Fail-closed attestation decisions through a scripted CLI's real stdio JSON-RPC."""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.services.umay import codex_bridge_policy as policy
from app.services.umay import codex_runtime_probe as probe
from app.services.umay.codex_runtime_rpc import ProbeError


class FileResponses:
    """The model I/O boundary only: the subprocess exchanges requests through files."""

    port = 1

    def __init__(self, root):
        self.root, self.queue, self.closed = root, [], False

    @property
    def requests(self):
        path = self.root / "requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def close(self):
        self.closed = True


@pytest.fixture
def scripted_probe(tmp_path, monkeypatch):
    provider = FileResponses(tmp_path)

    class Exchange:
        def __enter__(self):
            pass

        def __exit__(self, *exc):
            (tmp_path / "queue.json").write_text(json.dumps(provider.queue))

    provider.lock = Exchange()
    monkeypatch.setattr(probe, "SyntheticResponses", lambda: provider)
    monkeypatch.setattr(probe, "SETTLE_SECONDS", 0)  # transcript is flushed before turn/completed

    @contextmanager
    def listener():
        yield 1  # only the host socket boundary is replaced; CLI outcomes remain measured

    monkeypatch.setattr(probe, "loopback_control", listener)
    cli = Path(__file__).parent / "support" / "probe_cli.py"
    work = tmp_path / "work"

    def attest(mode="safe"):
        command = [sys.executable, str(cli), mode, str(tmp_path)]
        return probe.attest(command, cli, policy.bridge_config, work)

    yield attest
    assert list(work.iterdir()) == []  # pass and rejection both clean up the isolated CLI home
    assert provider.closed


def test_safe_scripted_cli_is_attested(scripted_probe):
    report = scripted_probe()
    assert report["passed"] is True and report["failures"] == []
    assert report["scenarios"]["health_archive"] == {
        "application_callbacks": 1,
        "result_delivered": True,
    }
    assert report["scenarios"]["sandbox"] == {
        "read_exit": 0,
        "read_ok": True,
        "write_exit": 1,
        "sentinel_unchanged": True,
        "listener_reachable_unsandboxed": True,
        "network_exit": 1,
        "network_connected": False,
    }
    assert report["runtime"]["cli_version"] == "0.161.0"
    assert {
        "image_absolute",
        "image_relative",
        "image_symlink",
        "image_exec",
        "fallback_image_absolute",
        "spawn_collaboration",
        "fallback_spawn_exec",
    } <= set(report["scenarios"])
    assert json.loads(report["catalog_text"])["models"] == [{"slug": "gpt-6.1-sol"}]


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("shell", "unexpected tools offered"),
        ("nested", "unexpected nested tools offered"),
        ("nested-runtime", "exec exposes unexpected tools"),
        (
            "missing-code-mode-host",
            "codex-code-mode-host may be missing: install the full Codex package",
        ),
        ("missing-health", "required tools missing"),
        ("config", "bridge configuration"),
        ("thread", "thread settings"),
        ("image", "image content reached a model request"),
        ("spawn-result", "a sub-agent was started"),
        ("spawn-thread", "unexpected thread(s) started"),
        ("spawn-request", "a sub-agent was started"),
        ("write-exit", "read-only sandbox allowed a write"),
        ("write-sentinel", "read-only sandbox allowed a write"),
        ("read", "sandbox positive control (read) failed"),
        ("network", "sandbox allowed a loopback network connection"),
        ("unanswered", "forced call produced no result"),
    ],
)
def test_unsafe_scripted_cli_is_never_attested(scripted_probe, mode, reason):
    with pytest.raises(ProbeError, match=reason.replace("(", r"\(").replace(")", r"\)")) as refused:
        scripted_probe(mode)
    report = refused.value.report
    assert report["passed"] is False
    assert any(reason in failure for failure in report["failures"])
    # Even the unsafe peers exercised the real callback in the same process.
    assert report["scenarios"]["health_archive"]["result_delivered"]
