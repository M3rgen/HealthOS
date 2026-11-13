"""The CLI split preserves the flat command contract and legacy dependency overrides."""

import asyncio
import importlib
import json
import logging
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import typer.rich_utils
from cli_records import cli_database as cli_database
from sqlalchemy.exc import StatementError
from typer.main import get_command
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import Settings
from app.models.state import BackfillJob, JobStatus, SyncState, WebhookEvent, WebhookNotification

RUNNER = CliRunner()
# Captured from the unsplit CLI: includes rendered help and every parameter's
# names, defaults, bounds, date formats, repeatability and help text.
CONTRACT = json.loads((Path(__file__).parent / "fixtures/cli_contract.json").read_text())
EXPORTS = {
    "archive_commands": (
        "verify history import_versions responses export_response SINCE_OPTION FROM_DB_OPTION "
        "FETCHED_SINCE_OPTION OUTPUT_OPTION"
    ),
    "identity_commands": (
        "identity_backfill identity_decide identity_sources DATA_TYPES_OPTION AFTER_OPTION"
    ),
    "sync_commands": "status backfill sync reconcile probe",
    "webhook_commands": "webhook_readiness webhook_status webhook_replay_pending",
    "rejection_commands": "rejections",
    "garmin_commands": "garmin_status garmin_pull garmin_backfill KINDS_OPTION",
    "google_commands": "google_rebind",
    "apple_commands": "apple_token apple_import apple_status apple_normalize",
    "demo_commands": "demo",
    "import_commands": "import_fit",
}
# Commands added after the contract was captured; each is tested where it lives.
LATER = ["google-rebind", "demo", "import-fit"]


def test_entry_point_and_flat_registration_are_unchanged():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert project["project"]["scripts"] == {"healthos": "app.cli.main:app"}
    assert not hasattr(cli, "legacy")
    assert not cli.app.registered_groups
    callbacks = [item.callback for item in cli.app.registered_commands]
    assert len(callbacks) == len(set(callbacks)) == 27
    assert [callback.__name__.replace("_", "-") for callback in callbacks] == list(
        CONTRACT["commands"]
    ) + LATER
    for module, names in EXPORTS.items():
        implementation = importlib.import_module(f"app.cli.{module}")
        for name in names.split():
            assert getattr(cli, name) is getattr(implementation, name)


@pytest.mark.parametrize("name", [None, *CONTRACT["commands"]])
def test_help_and_parameters_match_the_original_cli(name, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # Typer reads GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS once at import and then forces a colour
    # terminal, so on GitHub runners the help would carry ANSI codes the contract never had.
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)
    result = RUNNER.invoke(cli.app, [*([name] if name else []), "--help"], terminal_width=100)
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    if name is None:
        assert all(name in result.stdout for name in LATER)
        legacy_lines = []
        later_command = False
        for line in result.stdout.split("\n"):
            if any(line.startswith(f"│ {name} ") for name in LATER) or (
                later_command and line.startswith("│  ")
            ):
                later_command = True
                continue
            later_command = False
            legacy_lines.append(line)
        legacy_help = "\n".join(legacy_lines)
        assert legacy_help == CONTRACT["root_help"]
        return
    expected = CONTRACT["commands"][name]
    assert result.stdout == expected["help"]
    command = get_command(cli.app).commands[name]
    assert [
        {
            "name": p.name,
            "opts": p.opts,
            "secondary_opts": p.secondary_opts,
            "default": p.default,
            "required": p.required,
            "nargs": p.nargs,
            "multiple": p.multiple,
            "type": "path" if p.type.name == "path" else str(p.type),
            "help": getattr(p, "help", None),
            "is_flag": getattr(p, "is_flag", None),
        }
        for p in command.params
    ] == expected["parameters"]


@pytest.mark.parametrize("module", ["_context", *EXPORTS])
def test_command_modules_import_before_the_entry_point(module):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import app.cli.{module}; import sys; "
            "assert 'app.cli.main' not in sys.modules; import app.cli.main; "
            "assert len(app.cli.main.app.registered_commands) == 27",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_module_execution_still_exposes_the_same_commands():
    result = subprocess.run(
        [sys.executable, "-m", "app.cli.main", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert all(name in result.stdout for name in CONTRACT["commands"])


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize(
    ("arguments", "service", "method", "forwarded", "value", "expected"),
    [
        (["sync"], "incremental", "run_all", (), {"steps": 2}, "{'steps': 2}\n"),
        (["sync", "steps"], "incremental", "run", ("steps",), 2, "2\n"),
        (["reconcile"], "reconciliation", "run", (7,), {"steps": 2}, "{'steps': 2}\n"),
        (
            ["probe", "--days", "3"],
            "discovery",
            "probe",
            (3,),
            {"steps": 2},
            '{\n  "steps": 2\n}\n',
        ),
        (
            ["webhook-replay-pending"],
            "webhook_processor",
            "process_pending",
            (),
            SimpleNamespace(failed_elements=0),
            '{\n  "failed_elements": 0\n}\n',
        ),
        (["backfill"], "backfill", "run", (None,), 2, "Completed 2 backfill windows\n"),
    ],
)
def test_runtime_commands_keep_overrides_output_and_shutdown(
    cli_database, monkeypatch, fail, arguments, service, method, forwarded, value, expected
):
    failure = RuntimeError("synthetic service failure")
    operation = AsyncMock(return_value=value, side_effect=failure if fail else None)
    runtime = SimpleNamespace(
        **{service: SimpleNamespace(**{method: operation})}, shutdown=AsyncMock()
    )
    settings = SimpleNamespace(google_enabled=True)
    build = Mock(return_value=runtime)
    runner = Mock(side_effect=asyncio.run)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_runtime", build)
    monkeypatch.setattr(cli, "run", runner)

    result = RUNNER.invoke(cli.app, arguments)

    build.assert_called_once_with(settings, cli_database)
    operation.assert_awaited_once_with(*forwarded)
    runtime.shutdown.assert_awaited_once_with()
    assert runner.call_count == 1
    assert result.exit_code == int(fail)
    assert result.stdout == ("" if fail else expected)
    if fail:
        assert result.stderr == "Komut tamamlanamadı. Hata türü: RuntimeError\n"
    else:
        assert result.stderr == ""


@pytest.mark.parametrize("command", ["sync", "reconcile"])
def test_partial_service_failure_stays_on_stdout_and_returns_one(
    cli_database, monkeypatch, command
):
    values = {"steps": 2, "sleep": ValueError("synthetic")}
    runtime = SimpleNamespace(
        incremental=SimpleNamespace(run_all=AsyncMock(return_value=values)),
        reconciliation=SimpleNamespace(run=AsyncMock(return_value=values)),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(cli, "build_runtime", lambda *_: runtime)
    result = RUNNER.invoke(cli.app, [command])
    assert result.exit_code == 1
    assert result.stdout == f"{values}\n"
    assert result.stderr == ""
    runtime.shutdown.assert_awaited_once()


def test_webhook_replay_reports_failed_elements_before_exiting(cli_database, monkeypatch):
    runtime = SimpleNamespace(
        webhook_processor=SimpleNamespace(
            process_pending=AsyncMock(return_value=SimpleNamespace(failed_elements=2))
        ),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(cli, "build_runtime", lambda *_: runtime)
    result = RUNNER.invoke(cli.app, ["webhook-replay-pending"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"failed_elements": 2}
    assert result.stderr == ""
    runtime.shutdown.assert_awaited_once()


def test_backfill_scopes_reset_and_failed_jobs(cli_database, monkeypatch):
    async def seed():
        async with cli_database.begin() as session:
            for name, status in [("steps", JobStatus.abandoned), ("sleep", JobStatus.failed)]:
                session.add(
                    BackfillJob(
                        data_type=name,
                        status=status,
                        last_error="synthetic",
                        window_start=datetime(2026, 1, 1, tzinfo=UTC),
                        window_end=datetime(2026, 1, 2, tzinfo=UTC),
                    )
                )

    asyncio.run(seed())
    runtime = SimpleNamespace(
        backfill=SimpleNamespace(
            reset_abandoned=AsyncMock(return_value=1), run=AsyncMock(return_value=0)
        ),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(cli, "build_runtime", lambda *_: runtime)
    result = RUNNER.invoke(cli.app, ["backfill", "--reset-abandoned", "--data-type", "steps"])
    assert result.exit_code == 1
    assert (
        result.stdout == "Requeued 1 abandoned backfill window(s)\nCompleted 0 backfill windows\n"
    )
    assert result.stderr == "backfill abandoned: steps 2026-01-01 00:00:00: synthetic\n"
    runtime.backfill.reset_abandoned.assert_awaited_once_with(["steps"])
    runtime.backfill.run.assert_awaited_once_with(["steps"])
    runtime.shutdown.assert_awaited_once()


def test_webhook_and_sync_status_read_the_legacy_session_factory(cli_database, monkeypatch):
    when = datetime(2026, 1, 1, tzinfo=UTC)

    async def seed():
        async with cli_database.begin() as session:
            session.add(SyncState(data_type="steps", status=JobStatus.completed))
            session.add_all(
                [
                    WebhookEvent(payload_hash="a" * 64, raw_json={}, created_at=when),
                    WebhookEvent(
                        payload_hash="b" * 64, raw_json={}, created_at=when, processed_at=when
                    ),
                    WebhookNotification(
                        element_hash="c" * 64,
                        raw_json={},
                        status="failed",
                        sync_attempt_count=3,
                        sync_success_count=1,
                    ),
                ]
            )

    asyncio.run(seed())
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(webhook_readiness={"ready": False})
    )
    ready = RUNNER.invoke(cli.app, ["webhook-readiness"])
    assert ready.exit_code == 0 and ready.stderr == ""
    assert json.loads(ready.stdout) == {"ready": False}
    status = RUNNER.invoke(cli.app, ["webhook-status"])
    assert status.exit_code == 0 and status.stderr == ""
    assert json.loads(status.stdout) == {
        "readiness": {"ready": False},
        "received_events": 2,
        "pending_events": 1,
        "last_received_at": "2026-01-01 00:00:00",
        "last_processed_at": "2026-01-01 00:00:00",
        "notification_elements": 1,
        "notification_states": {"failed": 1},
        "incremental_sync_attempts": 3,
        "incremental_sync_successes": 1,
    }
    sync_status = RUNNER.invoke(cli.app, ["status"])
    assert sync_status.exit_code == 0 and sync_status.stderr == ""
    assert json.loads(sync_status.stdout)["sync"] == [
        {
            "data_type": "steps",
            "status": "completed",
            "last_successful_sync": "None",
        }
    ]


def test_verify_keeps_sync_errors_on_stderr(cli_database):
    async def seed():
        async with cli_database.begin() as session:
            session.add(SyncState(data_type="steps", last_error="synthetic"))

    asyncio.run(seed())
    result = RUNNER.invoke(cli.app, ["verify"])
    assert result.exit_code == 1
    assert result.stdout == (
        "Verified 0 raw records, 0 earlier versions, "
        "0 stored Google responses and 0 identity aliases\n"
    )
    assert result.stderr == "sync error: steps: synthetic\n"


@pytest.mark.parametrize("sql_error", [False, True])
def test_unexpected_cli_failure_has_safe_stderr_and_redacted_log(monkeypatch, caplog, sql_error):
    detail = (
        "synthetic-private-health-value /synthetic-private-dir/health-store.sqlite "
        "refresh_token=synthetic-private-token"
    )
    error = (
        StatementError(detail, "SELECT raw_json FROM health", {"value": detail}, ValueError(detail))
        if sql_error
        else RuntimeError(detail)
    )

    def fail():
        raise error

    monkeypatch.setattr(cli, "get_settings", fail)
    result = RUNNER.invoke(cli.app, ["sync"])
    assert result.exit_code == 1 and result.stdout == ""
    assert result.stderr == f"Komut tamamlanamadı. Hata türü: {type(error).__name__}\n"
    (record,) = [r for r in caplog.records if r.name == "app.cli._errors"]
    assert record.levelno == logging.ERROR and record.error_type == type(error).__name__
    assert record.error_frames and record.exc_info is None and record.exc_text is None
    emitted = result.output + caplog.text + repr(vars(record))
    assert all(
        value not in emitted
        for value in (
            detail,
            "synthetic-private",
            "/synthetic-private-dir/health-store.sqlite",
            "SELECT raw_json",
            "Traceback",
        )
    )


@pytest.mark.parametrize("error", [typer.BadParameter("synthetic"), typer.Exit(7), typer.Abort()])
def test_cli_adapter_preserves_typer_control_exceptions(monkeypatch, error):
    def fail():
        raise error

    monkeypatch.setattr(cli, "get_settings", fail)
    result = RUNNER.invoke(cli.app, ["sync"])
    assert result.exit_code == (
        7 if isinstance(error, typer.Exit) else 1 if isinstance(error, typer.Abort) else 2
    )
    assert "Komut tamamlanamadı" not in result.output


def test_actual_cli_entry_never_renders_unexpected_traceback():
    script = """
from app.cli import main
def fail():
    raise RuntimeError('SELECT raw_json; /private/health-store.sqlite; synthetic-private-token')
main.get_settings = fail
main.app()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "sync"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1 and result.stdout == ""
    assert "Komut tamamlanamadı. Hata türü: RuntimeError" in result.stderr
    assert "error_type=RuntimeError" in result.stderr and "error_frames=" in result.stderr
    assert all(
        value not in result.stderr
        for value in ("SELECT", "/private/", "synthetic-private", "Traceback", str(Path.cwd()))
    )


@pytest.mark.parametrize(
    "arguments",
    [["sync"], ["sync", "steps"], ["probe"], ["backfill", "--reset-abandoned"], ["reconcile"]],
)
@pytest.mark.parametrize("sources", ["none", "apple", "garmin"])
def test_google_commands_refuse_disabled_source_before_runtime(monkeypatch, arguments, sources):
    monkeypatch.setenv("HEALTHOS_SOURCES", sources)
    settings = Settings(_env_file=None, app_env="test")
    build = Mock(side_effect=AssertionError("disabled command must not build a runtime"))
    sessions = Mock(side_effect=AssertionError("disabled command must not access the database"))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_runtime", build)
    monkeypatch.setattr(cli, "SessionLocal", sessions)

    result = RUNNER.invoke(cli.app, arguments)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Bu kurulumda Google kaynağı kapalı.\n"
    build.assert_not_called()
    sessions.assert_not_called()
