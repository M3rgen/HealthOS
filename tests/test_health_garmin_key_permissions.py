"""Garmin key permissions warn without blocking startup or degrading pipeline health."""

import builtins
import logging
import secrets
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.core.config import Settings
from app.core.sources import garmin_key_mode, permissive_key_mode


@pytest.mark.parametrize("mode", [0o644, 0o600, 0o400, None])
def test_key_mode_uses_stat_only(tmp_path, monkeypatch, mode):
    path = tmp_path / "synthetic-key"
    if mode is not None:
        path.write_text("synthetic-test-key")
        path.chmod(mode)

    def denied(*args, **kwargs):
        raise AssertionError("Key contents must not be opened")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", denied)
        patch.setattr(Path, "open", denied)
        assert garmin_key_mode(path) == mode
    monkeypatch.setenv("HOME", str(tmp_path))
    assert garmin_key_mode(Path("~/synthetic-key")) == mode


@pytest.mark.parametrize("mode", [None, 0o600, 0o400, 0o700, 0o644, 0o640, 0o610, 0o602, 0o601])
def test_permissive_mode_checks_every_group_and_other_bit(mode):
    assert permissive_key_mode(mode) == (mode is not None and bool(mode & 0o077))


@pytest.mark.parametrize(
    "mode,enabled", [(0o644, True), (0o600, True), (0o400, True), (0o644, False)]
)
async def test_startup_warns_once_without_disclosing_key(
    tmp_path, monkeypatch, sessions, caplog, mode, enabled
):
    path = tmp_path / "synthetic-key"
    path.write_text("synthetic-test-key")
    path.chmod(mode)
    settings = Settings(
        _env_file=None,
        app_env="production",
        # make test-postgres connects as the disposable server's superuser, which production
        # refuses without this dated opt-in (app/core/role_policy.py); the role is not under test.
        healthos_allow_superuser_until=date.today() + timedelta(days=1),
        healthos_sources="garmin" if enabled else "none",
        garmin_api_key_file=path,
        database_url=f"postgresql+asyncpg://health:{secrets.token_urlsafe(48)}@localhost/health",
        app_ui_token=secrets.token_urlsafe(48),
        watchdog_enabled=True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    application = main.create_app()
    logging.getLogger().addHandler(caplog.handler)
    application.state.runtime.watchdog.startup_check = AsyncMock()
    async with application.router.lifespan_context(application):
        records = [
            r for r in caplog.records if getattr(r, "event", None) == "GARMIN_KEY_FILE_PERMISSIVE"
        ]
        assert len(records) == int(enabled and mode == 0o644)
        for record in records:
            assert record.levelname == "WARNING"
            assert record.mode == "644"
            for private in (str(path), path.name, "synthetic-test-key"):
                assert private not in record.getMessage()
                assert private not in repr(vars(record))


@pytest.mark.parametrize("enabled", [True, False])
async def test_health_permission_reason_tracks_chmod_without_changing_status(
    tmp_path, monkeypatch, sessions, enabled
):
    path = tmp_path / "synthetic-key"
    path.write_text("synthetic-test-key")
    settings = Settings(
        _env_file=None,
        app_env="test",
        healthos_sources="garmin" if enabled else "none",
        garmin_api_key_file=path,
        watchdog_enabled=False,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    application = main.create_app()
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://localhost"
        ) as client:
            reports = []
            for mode in (0o600, 0o644, 0o400):
                path.chmod(mode)
                for endpoint in ("/health", "/api/status"):
                    response = await client.get(endpoint)
                    assert response.status_code == 200
                    report = response.json()
                    assert ("garmin_key_file_permissive" in report["reasons"]) == (
                        enabled and mode == 0o644
                    )
                    reports.append(report)
            assert len({r["status"] for r in reports}) == 1
