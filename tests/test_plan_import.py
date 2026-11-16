"""CLI never echoes document contents, file names or rejected values."""

import subprocess
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from support.plans import SAMPLE, assert_private_output, raw, sample

from app.cli import plan_import
from app.models.plan import PlanVersion


def test_example_passes_real_module_check_without_database():
    result = subprocess.run(
        [sys.executable, "-m", "app.cli.plan_import", str(SAMPLE), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    assert "sections=9" in result.stdout and "shopping_items=2" in result.stdout
    assert_private_output(result.stdout)


@pytest.mark.parametrize("check", [False, True])
def test_invalid_file_never_opens_database_or_echoes_values(tmp_path, monkeypatch, capsys, check):
    store = AsyncMock()
    monkeypatch.setattr(plan_import, "store", store)
    path = tmp_path / "private-file-name.json"
    document = sample()
    document["day_types"][0]["meals"][0]["protein_g"] = "private rejected value"
    path.write_bytes(raw(document))
    assert plan_import.main([str(path), *(["--check"] if check else [])]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "$.day_types[0].meals[0].protein_g" in captured.err
    assert "private" not in captured.err
    assert_private_output(captured.err)
    store.assert_not_called()


@pytest.mark.parametrize("check", [False, True])
def test_success_has_one_content_free_summary(monkeypatch, capsys, check):
    store = AsyncMock(return_value=(7, True, True))
    monkeypatch.setattr(plan_import, "store", store)
    assert plan_import.main([str(SAMPLE), *(["--check"] if check else [])]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and len(captured.out.splitlines()) == 1
    assert "sessions=1" in captured.out
    assert_private_output(captured.out)
    assert store.call_count == (0 if check else 1)
    if not check:
        assert "Version 7: added=1 active=1" in captured.out


def test_io_and_database_errors_do_not_echo_exception_text(tmp_path, monkeypatch, capsys):
    assert plan_import.main([str(tmp_path / "private-file-name")]) == 2
    assert "private" not in capsys.readouterr().err
    monkeypatch.setattr(plan_import, "store", AsyncMock(side_effect=RuntimeError("private value")))
    assert plan_import.main([str(SAMPLE)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "RuntimeError" in captured.err and "private" not in captured.err
    assert_private_output(captured.err)


async def test_cli_storage_commits_and_logs_only_version_and_counts(sessions, monkeypatch, caplog):
    import app.db.session

    @asynccontextmanager
    async def lifespan(*args):
        yield sessions

    monkeypatch.setattr(app.db.session, "database_lifespan", lifespan)
    caplog.clear()
    first, added, active = await plan_import.store(sample())
    assert added and active
    newer, added, active = await plan_import.store({"schema": 1})
    assert newer > first and added and active
    second, added, active = await plan_import.store(sample())
    assert second == first and not added and not active
    async with sessions() as session:
        row = await session.get(PlanVersion, first)
        assert row.content == sample()
    records = [r for r in caplog.records if r.name == "app.services.operations.plans"]
    assert [(r.version, r.added, r.sections) for r in records] == [
        (first, True, 9),
        (newer, True, 0),
        (first, False, 9),
    ]
    # The checkout path is not log content: a directory name may contain a short sample value.
    fields = [{k: v for k, v in r.__dict__.items() if k != "pathname"} for r in records]
    assert_private_output(caplog.text + str(fields))
