"""Synthetic FIT imports share the API core and retain CLI transaction boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cli_records import cli_database as cli_database
from fit_factory import START, activity
from sqlalchemy import func, select
from typer.testing import CliRunner

from app import main
from app.cli import main as cli
from app.core.config import Settings
from app.db.session import get_session
from app.models.health import ExerciseSession
from app.models.state import RawHealthData
from app.services.providers.fit_decode import FitError
from app.services.providers.fit_import import import_fit_bytes

RUNNER = CliRunner()


@pytest.fixture
def runtime(cli_database, monkeypatch):
    runtime = SimpleNamespace(shutdown=AsyncMock())
    monkeypatch.setattr(cli, "build_runtime", lambda settings, sessions: runtime)
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, healthos_sources="none")
    )
    return runtime


def test_import_fit_help():
    assert "import-fit" in RUNNER.invoke(cli.app, ["--help"]).stdout
    help_result = RUNNER.invoke(cli.app, ["import-fit", "--help"])
    assert help_result.exit_code == 0
    assert "FIT" in help_result.stdout


@pytest.mark.parametrize("manufacturer, source", [(1, "GARMIN"), (255, "FIT")])
def test_import_fit_commits_and_reupload_keeps_one_workout(
    runtime, cli_database, tmp_path, monkeypatch, manufacturer, source
):
    path = tmp_path / "activity.fit"
    path.write_bytes(activity(manufacturer=manufacturer, elapsed=1800000, duration=1800000))
    settings = Settings(_env_file=None, healthos_sources="none", app_ui_token="")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    application = main.create_app()

    async def session_dep():
        async with cli_database() as session:
            yield session

    application.dependency_overrides[get_session] = session_dep

    async def dashboard():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://localhost"
        ) as http:
            response = await http.get(
                "/api/dashboard/workouts", params={"month": START.date().isoformat()}
            )
            assert response.status_code == 200, response.text
            rows = response.json()
            assert len(rows) == 1
            assert 1700 <= rows[0]["duration_seconds"] <= 1900
            assert rows[0]["source_code"] == rows[0]["provider"] == source
            return rows

    first = RUNNER.invoke(cli.app, ["import-fit", str(path)])
    assert first.exit_code == 0, first.output
    result = json.loads(first.stdout)
    assert set(result) == {"sha256", "records", "inserted", "updated", "unchanged"}
    assert result["inserted"] == result["records"] == 3
    before = asyncio.run(dashboard())
    second = RUNNER.invoke(cli.app, ["import-fit", str(path)])
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout) == {**result, "inserted": 0, "unchanged": 3}
    assert asyncio.run(dashboard()) == before

    async def check():
        async with cli_database() as session:
            assert await session.scalar(select(func.count()).select_from(ExerciseSession)) == 1

    asyncio.run(check())
    asyncio.run(application.state.runtime.shutdown())
    assert runtime.shutdown.await_count == 2


@pytest.mark.parametrize("data", [b"not-fit", b"", activity()[:-1], b"x" * 257])
def test_import_fit_rejects_without_writes(runtime, cli_database, tmp_path, monkeypatch, data):
    import app.cli.import_commands as command
    import app.services.providers.fit_import as service

    monkeypatch.setattr(command, "MAX_FIT_BYTES", 256)
    monkeypatch.setattr(service, "MAX_FIT_BYTES", 256)
    path = tmp_path / "invalid.fit"
    path.write_bytes(data)
    result = RUNNER.invoke(cli.app, ["import-fit", str(path)])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "FIT" in result.stderr
    if len(data) > 256:
        assert result.stderr == "FIT dosyası çok büyük.\n"

    async def check():
        async with cli_database() as session:
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0

    asyncio.run(check())
    runtime.shutdown.assert_awaited_once_with()


def test_import_fit_matches_api_on_independent_archives(
    runtime, cli_database, sessions, tmp_path, monkeypatch
):
    settings = Settings(_env_file=None, healthos_sources="none", app_ui_token="")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    application = main.create_app()

    async def session_dep():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session_dep
    data = activity()
    path = tmp_path / "activity.fit"
    path.write_bytes(data)
    result = RUNNER.invoke(cli.app, ["import-fit", str(path)])
    assert result.exit_code == 0, result.output

    async def upload():
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(application), base_url="http://localhost"
            ) as http:
                response = await http.post(
                    "/api/imports/garmin-fit",
                    content=data,
                    headers={"Content-Type": "application/octet-stream", "X-HealthOS-Request": "1"},
                )
                assert response.status_code == 200, response.text
                return response.json()
        finally:
            await application.state.runtime.shutdown()

    assert asyncio.run(upload()) == json.loads(result.stdout)


async def test_shared_core_rejects_oversize_before_parsing(sessions, monkeypatch):
    import app.services.providers.fit_import as service

    parse = AsyncMock()
    monkeypatch.setattr(service, "MAX_FIT_BYTES", 1)
    monkeypatch.setattr(service.asyncio, "to_thread", parse)
    async with sessions() as session:
        with pytest.raises(FitError, match="FIT dosyası çok büyük"):
            await import_fit_bytes(session, b"xx")
    parse.assert_not_awaited()


def test_import_fit_rolls_back_on_write_failure(runtime, cli_database, tmp_path, monkeypatch):
    import app.services.providers.fit_import as service

    original = service.store_records

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic archive write failure")

    monkeypatch.setattr(service, "store_records", fail_after_write)
    path = tmp_path / "activity.fit"
    path.write_bytes(activity())
    result = RUNNER.invoke(cli.app, ["import-fit", str(path)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Komut tamamlanamadı. Hata türü: RuntimeError\n"

    async def check():
        async with cli_database() as session:
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0

    asyncio.run(check())
    runtime.shutdown.assert_awaited_once_with()
