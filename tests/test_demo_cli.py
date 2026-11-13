"""``healthos demo``: loads the sample, says what it is, removes it, refuses real archives."""

import asyncio
import json
from datetime import UTC, datetime

from cli_records import cli_database as cli_database
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.providers import GARMIN
from app.models.health import Steps

RUNNER = CliRunner()


def test_load_then_remove(cli_database):
    loaded = RUNNER.invoke(cli.app, ["demo", "--timezone", "Europe/Istanbul"])
    assert loaded.exit_code == 0, loaded.output
    summary = json.loads(loaded.stdout)
    assert summary["loaded"] is True and summary["added"]["steps"] == 90
    assert "healthos demo --remove" in loaded.stderr
    removed = RUNNER.invoke(cli.app, ["demo", "--remove"])
    assert removed.exit_code == 0, removed.output
    after = json.loads(removed.stdout)
    assert sum(after["removed"].values()) == summary["rows"]
    assert after["loaded"] is False and after["rows"] == 0


def test_an_archive_with_device_data_is_refused(cli_database):
    async def add():
        async with cli_database.begin() as session:
            session.add(
                Steps(
                    google_data_point_id="garmin:steps:2026-11-10:v2",
                    provider=GARMIN,
                    source_record_id="steps:2026-11-10",
                    start_time=datetime(2026, 11, 10, tzinfo=UTC),
                    end_time=datetime(2026, 11, 11, tzinfo=UTC),
                    count=4321,
                )
            )

    asyncio.run(add())
    result = RUNNER.invoke(cli.app, ["demo"])
    assert result.exit_code == 1
    assert "gerçek veri" in result.stderr and result.stdout == ""


def test_unknown_time_zone_exits_two(cli_database):
    result = RUNNER.invoke(cli.app, ["demo", "--timezone", "Mars/Olympus"])
    assert result.exit_code == 2
    assert "Saat dilimi" in result.stderr
