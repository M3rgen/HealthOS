"""The smoke harness leaves nothing behind and ignores the checkout's own .env."""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import verify_compose as verifier

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def fake_docker(listing: str, calls: list[tuple[str, ...]]):
    def docker(*arguments, env=None, capture=False, check=True):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout=listing, stderr="")

    return docker


def test_only_the_tags_this_run_built_are_removed(monkeypatch):
    calls: list[tuple[str, ...]] = []
    run = verifier.PROJECT_NAME
    listing = (
        f"{run}-app:latest\n{run}-existing-migrate:latest\n"
        "healthos-smoke-1-deadbeef-app:latest\nhealthos:ci\n"
    )
    monkeypatch.setattr(verifier, "docker", fake_docker(listing, calls))
    verifier.remove_run_images({})
    assert calls[0][:2] == ("image", "ls")
    assert f"reference={run}-*" in calls[0]
    assert calls[1] == ("image", "rm", f"{run}-app:latest", f"{run}-existing-migrate:latest")


def test_nothing_is_removed_when_the_run_built_nothing(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(verifier, "docker", fake_docker("", calls))
    verifier.remove_run_images({})
    assert len(calls) == 1


def test_compose_reads_an_empty_file_instead_of_the_checkout_env():
    source = (ROOT / "scripts" / "verify_compose.py").read_text(encoding="utf-8")
    assert '"COMPOSE_ENV_FILES": str(SMOKE_NO_ENV_FILE)' in source
    assert verifier.SMOKE_NO_ENV_FILE.name.startswith(".env.")


@pytest.mark.parametrize(
    ("expected", "reasons"),
    [
        ({"status": "missing", "last_attempt": None}, ["backup_missing"]),
        ({"status": "available", "last_attempt": {"result": "success"}}, []),
        ({"status": "available", "last_attempt": {"result": "failure"}}, ["backup_failed"]),
    ],
)
def test_the_smoke_checks_the_health_reason_of_each_backup_state(expected, reasons):
    assert verifier.expected_backup_reasons(expected) == reasons


def test_postgres_gets_time_to_finish_a_fast_shutdown():
    assert COMPOSE["services"]["postgres"]["stop_grace_period"] == "60s"


def test_bridge_unit_names_the_interpreter_trap_before_starting():
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    unit.read(ROOT / "deploy/systemd/healthos-bridge.service")
    check = unit["Service"]["ExecStartPre"]
    # `$$` is systemd's literal dollar; a single `$` would be expanded (to nothing) by systemd.
    assert '"$$(readlink -f /opt/healthos/.venv/bin/python)"' in check
    assert "UV_PYTHON_INSTALL_DIR=/opt/healthos/.python" in check


def test_bridge_unit_allows_exactly_the_sandbox_address_families():
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.read(ROOT / "deploy/systemd/healthos-bridge.service")
    service = unit["Service"]
    assert set(service["RestrictAddressFamilies"].split()) == {
        "AF_UNIX",
        "AF_INET",
        "AF_INET6",
        "AF_NETLINK",
    }
    assert service["Type"] == "notify" and service["NotifyAccess"] == "main"
