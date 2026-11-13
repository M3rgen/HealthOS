"""Exercise the registered Garmin commands with an offline pull service."""

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cli_records import cli_database as cli_database
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import Settings

RUNNER = CliRunner()
COMMANDS = (["garmin-status"], ["garmin-pull"], ["garmin-backfill", "--since", "2026-08-10"])


@pytest.fixture
def runtime(cli_database, monkeypatch):
    fake = SimpleNamespace(
        garmin=SimpleNamespace(status=AsyncMock(), run=AsyncMock(), backfill=AsyncMock()),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "build_runtime", lambda *_: fake)
    return fake


@pytest.mark.parametrize("arguments", COMMANDS)
def test_missing_garmin_exits_two_and_shuts_down(runtime, arguments):
    runtime.garmin = None
    result = RUNNER.invoke(cli.app, arguments)
    assert result.exit_code == 2
    assert result.stderr == "Garmin entegrasyonu bu çalışma zamanında yok.\n"
    assert result.stdout == ""
    runtime.shutdown.assert_awaited_once_with()


def test_status_prints_service_json_and_shuts_down(runtime):
    report = {"enabled": True, "last_pull": date(2026, 8, 11), "kinds": {"steps": {"records": 3}}}
    runtime.garmin.status.return_value = report
    result = RUNNER.invoke(cli.app, ["garmin-status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {**report, "last_pull": "2026-08-11"}
    runtime.garmin.status.assert_awaited_once_with()
    runtime.shutdown.assert_awaited_once_with()


@pytest.mark.parametrize("kinds", [None, ["steps"], ["steps", "activities"]])
@pytest.mark.parametrize("failed", [False, True])
def test_pull_forwards_only_requested_kinds_and_reports_failure(runtime, kinds, failed):
    report = {"failed": failed, "records": 3}
    runtime.garmin.run.return_value = SimpleNamespace(failed=failed, as_dict=lambda: report)
    arguments = ["garmin-pull"]
    for kind in kinds or []:
        arguments += ["--kind", kind]
    result = RUNNER.invoke(cli.app, arguments)
    assert result.exit_code == int(failed)
    assert json.loads(result.stdout) == report
    runtime.garmin.run.assert_awaited_once_with(kinds)
    runtime.shutdown.assert_awaited_once_with()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_backfill_forwards_dates_kinds_and_dry_run(runtime, explicit, failed):
    report = {"failed": failed, "requests": 4}
    runtime.garmin.backfill.return_value = report
    arguments = ["garmin-backfill", "--since", "2026-08-10"]
    if explicit:
        arguments += [
            "--until",
            "2026-08-11",
            "--kind",
            "activities",
            "--kind",
            "steps",
            "--dry-run",
        ]
    result = RUNNER.invoke(cli.app, arguments)
    assert result.exit_code == int(failed)
    assert json.loads(result.stdout) == report
    runtime.garmin.backfill.assert_awaited_once_with(
        date(2026, 8, 10),
        date(2026, 8, 11) if explicit else None,
        kinds=["activities", "steps"] if explicit else None,
        dry_run=explicit,
    )
    runtime.shutdown.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--since", "invalid"], "Tarih biçimi YYYY-AA-GG olmalı:"),
        (["--since", "2026-08-10", "--until", "invalid"], "Tarih biçimi YYYY-AA-GG olmalı:"),
        (
            ["--since", "2026-08-10", "--until", "2026-08-09"],
            "--until, --since gününden önce olamaz.",
        ),
    ],
)
def test_invalid_backfill_dates_exit_before_building_runtime(monkeypatch, options, message):
    build = Mock()
    monkeypatch.setattr(cli, "build_runtime", build)
    result = RUNNER.invoke(cli.app, ["garmin-backfill", *options])
    assert result.exit_code == 2
    assert message in result.stderr
    build.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "method"), zip(COMMANDS, ("status", "run", "backfill"), strict=True)
)
def test_service_errors_still_shut_down(runtime, arguments, method):
    getattr(runtime.garmin, method).side_effect = RuntimeError("synthetic service failure")
    result = RUNNER.invoke(cli.app, arguments)
    assert result.exit_code == 1
    assert result.stderr == "Komut tamamlanamadı. Hata türü: RuntimeError\n"
    runtime.shutdown.assert_awaited_once_with()
