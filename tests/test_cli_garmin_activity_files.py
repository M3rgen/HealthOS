"""The backfill CLI prints a count, never paths, ids, file names or errors from Garmin."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cli import garmin_activity_files as cli
from app.services.providers.garmin_activity_files import FilePullResult


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_forwards_limit_and_prints_only_count(monkeypatch, capsys, dry_run):
    run = AsyncMock(return_value=FilePullResult(requests=7, downloaded=4, unavailable=1))
    monkeypatch.setattr(cli, "_run", run)
    assert cli.main(["--limit", "7"] + (["--dry-run"] if dry_run else [])) == 0
    run.assert_awaited_once_with(7, dry_run)
    output = capsys.readouterr()
    assert output.out == ("7\n" if dry_run else "5\n") and not output.err


def test_cli_partial_failure_is_nonzero_and_keeps_count(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_run",
        AsyncMock(
            return_value=FilePullResult(
                requests=3,
                downloaded=2,
                failed=1,
                stopped=True,
            )
        ),
    )
    assert cli.main(["--limit", "10"]) == 1
    assert capsys.readouterr().out == "2\n"


def test_cli_exception_text_never_appears(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run", AsyncMock(side_effect=RuntimeError("SYNTHETIC-PRIVATE-DATA")))
    assert cli.main(["--limit", "10"]) == 1
    output = capsys.readouterr()
    assert output.out == "0\n"
    assert output.err == "garmin_activity_files_failed\n"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "abc"])
def test_cli_limit_must_be_a_positive_integer(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--limit", value])
    assert exc.value.code == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_uses_the_runtime_garmin_owner_and_always_shuts_down(monkeypatch, capsys, enabled):
    garmin = SimpleNamespace(activity_files=AsyncMock(return_value=FilePullResult(downloaded=3)))
    runtime = SimpleNamespace(garmin=garmin if enabled else None, shutdown=AsyncMock())
    monkeypatch.setattr(cli.context, "runtime", lambda: runtime)
    assert cli.main(["--limit", "4"]) == int(not enabled)
    assert capsys.readouterr().out == ("3\n" if enabled else "0\n")
    runtime.shutdown.assert_awaited_once()
    if enabled:
        garmin.activity_files.assert_awaited_once_with(4, dry_run=False)
