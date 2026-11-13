"""Readiness belongs to the launcher, after the probe and authenticated front startup."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from scripts import codex_bridge_service as service
from scripts import start_codex_bridge as bridge


@pytest.mark.parametrize("address", ["/run/bridge-notify.sock", "@bridge-notify"])
def test_ready_is_one_unix_datagram(monkeypatch, address):
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    factory = MagicMock()
    monkeypatch.setattr(service.socket, "socket", factory)
    service.notify_ready()
    factory.assert_called_once_with(service.socket.AF_UNIX, service.socket.SOCK_DGRAM)
    expected = "\0bridge-notify" if address.startswith("@") else address
    factory.return_value.__enter__.return_value.sendto.assert_called_once_with(b"READY=1", expected)
    factory.return_value.__exit__.assert_called_once()


def test_manual_launch_needs_no_notification_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    factory = Mock(side_effect=AssertionError("no socket needed"))
    monkeypatch.setattr(service.socket, "socket", factory)
    service.notify_ready()
    factory.assert_not_called()


@pytest.mark.parametrize("outcome", ["ready", "timeout", "exit", "notify-error", "manual"])
def test_supervisor_waits_for_front_and_cleans_up_on_failure(monkeypatch, outcome):
    if outcome == "manual":
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    else:
        monkeypatch.setenv("NOTIFY_SOCKET", "/synthetic/notify.sock")
    process = Mock(pid=12345, returncode=7)
    # The front initially refuses connections; notify only after a successful handshake.
    process.poll.side_effect = (
        [None, None, None, 7]
        if outcome in {"ready", "notify-error"}
        else [None, 7]
        if outcome in {"timeout", "manual"}
        else [7]
    )
    monkeypatch.setattr(service.subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(service.os, "killpg", kill)
    clock = Mock(side_effect=[0, 41] if outcome == "timeout" else [0, 0])
    monkeypatch.setattr(service, "time", SimpleNamespace(sleep=lambda _: None, monotonic=clock))
    ready = AsyncMock(side_effect=[False, True])
    notifications = []

    def notify():
        assert ready.await_count == 2
        assert process.poll.call_count == 3
        notifications.append(b"READY=1")
        if outcome == "notify-error":
            raise OSError("notification failed")

    monkeypatch.setattr(service, "notify_ready", notify)
    if outcome == "notify-error":
        with pytest.raises(OSError, match="notification failed"):
            service.supervise(["synthetic-front"], None, {}, ready)
    else:
        result = service.supervise(["synthetic-front"], None, {}, ready)
        assert result == (1 if outcome == "timeout" else 7)
    assert notifications == ([b"READY=1"] if outcome in {"ready", "notify-error"} else [])
    assert ready.await_count == (
        0 if outcome in {"exit", "manual"} else 1 if outcome == "timeout" else 2
    )
    assert kill.call_count == 2 and process.wait.call_count == 2


def test_failed_runtime_probe_never_notifies(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "ROOT", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "identity"))
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "synthetic-bridge-token-0123456789-abcdef")
    monkeypatch.setenv("NOTIFY_SOCKET", "/synthetic/notify.sock")
    monkeypatch.setattr(sys, "argv", ["start_codex_bridge.py", "--foreground"])
    monkeypatch.setattr(bridge, "bind_host", lambda: "127.0.0.1")
    monkeypatch.setattr(bridge, "listening", lambda _: False)
    monkeypatch.setattr(bridge, "select_cli", lambda _: (["synthetic-cli"], tmp_path / "cli"))
    monkeypatch.setattr(bridge, "verify_runtime", Mock(side_effect=SystemExit(1)))
    notify = Mock()
    monkeypatch.setattr(service, "notify_ready", notify)
    with pytest.raises(SystemExit) as result:
        bridge.main()
    assert result.value.code == 1
    notify.assert_not_called()
