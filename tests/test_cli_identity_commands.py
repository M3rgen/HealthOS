"""Identity commands retain dry runs, progress channels and refusal handling."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cli_records import cli_database as cli_database
from sqlalchemy import select
from support.identity import archive_with_duplicates
from typer.testing import CliRunner

from app.cli import main as cli
from app.db.base import Base
from app.services.identities import identity_backfill, identity_decisions
from app.services.identities.identity_backfill import LockUnavailable
from app.services.identities.identity_backfill_stats import BackfillStats
from app.services.identities.identity_decisions import DecisionRefused, Outcome
from app.services.identities.identity_options import SourceDecision
from app.services.identities.source_chain_locks import BackfillRunning

RUNNER = CliRunner()


@pytest.mark.parametrize("incremental", [False, True])
def test_identity_backfill_dry_run_leaves_the_archive_unchanged(cli_database, incremental):
    asyncio.run(archive_with_duplicates(cli_database))

    async def snapshot():
        async with cli_database() as session:
            return {
                table.name: (await session.execute(select(table))).all()
                for table in Base.metadata.sorted_tables
            }

    before = asyncio.run(snapshot())
    arguments = ["identity-backfill", "--dry-run", "--data-type", "steps"]
    if incremental:
        arguments.append("--incremental")
    result = RUNNER.invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["total"]["superseded"] == 1
    assert ("resume: --data-type steps --after" in result.stderr) is not incremental
    assert asyncio.run(snapshot()) == before


@pytest.mark.parametrize("error", [BackfillRunning, LockUnavailable])
@pytest.mark.parametrize("command", ["identity-backfill", "identity-decide"])
def test_busy_identity_commands_keep_refusals_on_stderr(cli_database, monkeypatch, error, command):
    arguments = [command]
    module, name = identity_backfill, "identity_backfill"
    if command == "identity-decide":
        arguments.extend(["link", "--data-type", "steps", "--from", "abcdef", "--to", "123456"])
        module, name = identity_decisions, "decide_sources"
    monkeypatch.setattr(module, name, AsyncMock(side_effect=error("busy")))
    result = RUNNER.invoke(cli.app, arguments)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"{command} refused: busy\n"


def test_identity_backfill_keeps_report_before_per_type_errors(cli_database, monkeypatch):
    report = {
        "sleep": BackfillStats(rows=1, error="synthetic"),
        "steps": BackfillStats(rows=2),
    }

    async def backfill(sessions, **kwargs):
        assert sessions is cli_database
        assert kwargs["after"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert kwargs["data_types"] == ["steps", "sleep"]
        kwargs["options"].notice("synthetic notice")
        kwargs["options"].progress("steps", report["steps"], datetime(2026, 1, 2, tzinfo=UTC))
        return report

    monkeypatch.setattr(identity_backfill, "identity_backfill", backfill)
    result = RUNNER.invoke(
        cli.app,
        [
            "identity-backfill",
            "--data-type",
            "steps",
            "--data-type",
            "sleep",
            "--after",
            "2026-01-01",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["total"]["rows"] == 3
    assert result.stderr == (
        "synthetic notice\nsteps: 2 rows, 0 superseded, up to 2026-01-02T00:00:00+00:00"
        " (resume: --data-type steps --after 2026-01-02T00:00:00)\n"
        "identity-backfill: sleep failed (the others ran): synthetic\n"
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_identity_decide_forwards_options_and_reports_progress(cli_database, monkeypatch, dry_run):
    outcome = Outcome("link", "steps", {}, {}, dry_run)

    async def decide(sessions, *args, **kwargs):
        assert sessions is cli_database
        assert args == ("steps",)
        assert kwargs["request"] == SourceDecision("link", "abcdef", "123456", "synthetic note")
        assert kwargs["options"].dry_run is dry_run
        assert kwargs["options"].batch_size == 100
        kwargs["options"].notice("synthetic notice")
        kwargs["options"].progress("steps", BackfillStats(rows=2), datetime(2026, 1, 2, tzinfo=UTC))
        return outcome

    monkeypatch.setattr(identity_decisions, "decide_sources", decide)
    args = [
        "identity-decide",
        "link",
        "--data-type",
        "steps",
        "--from",
        "abcdef",
        "--to",
        "123456",
        "--note",
        "synthetic note",
        "--batch-size",
        "100",
    ]
    result = RUNNER.invoke(cli.app, [*args, *(["--dry-run"] if dry_run else [])])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == outcome.as_dict()
    assert result.stderr == (
        "synthetic notice\nsteps: 2 rows, 0 superseded, up to 2026-01-02T00:00:00+00:00\n"
    )


def test_identity_decide_invalid_sources_are_still_refused(cli_database):
    result = RUNNER.invoke(
        cli.app,
        [
            "identity-decide",
            "link",
            "--data-type",
            "steps",
            "--from",
            "abc",
            "--to",
            "123",
        ],
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert (
        result.stderr == "identity-decide refused: give at least 6 characters of the fingerprint\n"
    )


@pytest.mark.parametrize("failure", [False, True])
def test_identity_sources_retains_json_and_service_failures(cli_database, monkeypatch, failure):
    error = DecisionRefused("synthetic")
    report = {"data_type": "steps", "at": datetime(2026, 1, 1, tzinfo=UTC)}
    read = AsyncMock(return_value=report, side_effect=error if failure else None)
    monkeypatch.setattr(identity_decisions, "list_sources", read)
    result = RUNNER.invoke(cli.app, ["identity-sources", "--data-type", "steps"])
    read.assert_awaited_once_with(cli_database, "steps")
    assert result.exit_code == int(failure)
    if failure:
        assert result.stdout == ""
        assert result.stderr == "Komut tamamlanamadı. Hata türü: DecisionRefused\n"
    else:
        assert result.stderr == ""
        assert result.stdout == json.dumps(report, indent=2, default=str) + "\n"


def test_identity_backfill_by_hand_waits_the_configured_minutes_for_a_busy_lock(
    cli_database, monkeypatch
):
    """A sync holding a data type (the watchdog's recovery after a restart) is waited for
    IDENTITY_RECONCILE_LOCK_WAIT_MINUTES; a dry run takes no lock and waits for nothing."""
    from app.cli._context import context

    seen = []

    async def backfill(sessions, **kwargs):
        seen.append(kwargs["lock_deadline"])
        return {}

    monkeypatch.setattr(identity_backfill, "identity_backfill", backfill)
    settings = context.settings().model_copy(update={"identity_reconcile_lock_wait_minutes": 30})
    monkeypatch.setattr(context, "settings", lambda: settings)
    before = datetime.now(UTC)
    assert RUNNER.invoke(cli.app, ["identity-backfill", "--incremental"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["identity-backfill", "--dry-run"]).exit_code == 0
    waited, dry = seen
    assert dry is None
    assert before + timedelta(minutes=30) <= waited <= datetime.now(UTC) + timedelta(minutes=30)
