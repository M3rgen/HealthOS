"""Source selection, production startup and registration contracts (synthetic data only)."""

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import main
from app.core.config import Settings
from app.db.session import get_session
from app.models.state import SyncState
from app.services.synchronization.scheduler import build_scheduler
from scripts.verify_configuration import check_environment

PRODUCTION = {
    "app_env": "production",
    # make test-postgres connects as the disposable server's superuser, which production
    # refuses without this dated opt-in (app/core/role_policy.py).
    "healthos_allow_superuser_until": date.today() + timedelta(days=1),
    "watchdog_enabled": True,  # ci.yml exports WATCHDOG_ENABLED=false
    "database_url": "postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health",
    "app_ui_token": "synthetic-ui-token-0123456789abcdef",
    "google_client_id": "",
    "google_client_secret": "",
    "google_token_encryption_key": "",
    "google_health_webhook_authorization": "",
    "google_health_webhook_endpoint_uri": "",
}


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "synthetic-key"
    path.write_text("synthetic-test-key", encoding="utf-8")
    return path


def test_source_environment_and_legacy_precedence(monkeypatch):
    assert Settings(_env_file=None).healthos_sources == ["google"]
    legacy = Settings(_env_file=None, garmin_enabled=True)
    assert legacy.healthos_sources == ["google", "garmin"]
    monkeypatch.setenv("HEALTHOS_SOURCES", " garmin, apple,garmin ")
    selected = Settings(_env_file=None, garmin_enabled=False)
    assert selected.healthos_sources == ["garmin", "apple"]
    assert selected.garmin_enabled
    monkeypatch.setenv("HEALTHOS_SOURCES", "none")
    assert Settings(_env_file=None, garmin_enabled=True).healthos_sources == []
    assert not Settings(_env_file=None, garmin_enabled=True).garmin_enabled
    monkeypatch.setenv("HEALTHOS_SOURCES", "google")
    assert not Settings(_env_file=None, garmin_enabled=True).garmin_enabled


@pytest.mark.parametrize("selection", ["unknown", "none,google", "none,garmin", "", "google,"])
def test_invalid_source_selection_fails_startup(selection):
    with pytest.raises(ValidationError, match="HEALTHOS_SOURCES"):
        Settings(_env_file=None, healthos_sources=selection)


def test_google_still_requires_its_production_configuration():
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None, healthos_sources="google", **PRODUCTION)
    for name in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_TOKEN_ENCRYPTION_KEY",
        "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION",
        "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI",
    ):
        assert name in str(failure.value)


def test_source_free_production_keeps_shared_secret_guards():
    for overrides, field in (
        ({"database_url": "postgresql+asyncpg://health:health@localhost/health"}, "DATABASE_URL"),
        ({"database_url": ""}, "DATABASE_URL"),
        ({"app_ui_token": ""}, "APP_UI_TOKEN"),
    ):
        with pytest.raises(ValidationError, match=field):
            Settings(_env_file=None, healthos_sources="none", **(PRODUCTION | overrides))


def test_garmin_production_requires_readable_key_file(tmp_path, monkeypatch, key_file):
    options = PRODUCTION | {"healthos_sources": "garmin"}
    for path in (tmp_path / "missing", tmp_path):
        with pytest.raises(ValidationError, match="GARMIN_API_KEY_FILE"):
            Settings(_env_file=None, garmin_api_key_file=path, **options)

    def denied(*args, **kwargs):
        raise PermissionError("synthetic denial")

    with monkeypatch.context() as context:
        context.setattr(Path, "open", denied)
        with pytest.raises(ValidationError, match="GARMIN_API_KEY_FILE"):
            Settings(_env_file=None, garmin_api_key_file=key_file, **options)
    key_file.write_bytes(b"")
    with pytest.raises(ValidationError, match="GARMIN_API_KEY_FILE"):
        Settings(_env_file=None, garmin_api_key_file=key_file, **options)


@pytest.mark.parametrize("selection", ["garmin", "none", "garmin,apple", "apple"])
async def test_google_free_production_lifespan_routes_jobs_and_status(
    selection, key_file, sessions, monkeypatch
):
    settings = Settings(
        _env_file=None, healthos_sources=selection, garmin_api_key_file=key_file, **PRODUCTION
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    recover = AsyncMock(side_effect=AssertionError("Google recovery must not run"))
    monkeypatch.setattr(main, "recover_stale_running", recover)
    application = main.create_app()
    runtime = application.state.runtime
    runtime.webhook_status.start = AsyncMock()
    runtime.watchdog.startup_check = AsyncMock()
    runtime.discovery.devices = AsyncMock(side_effect=AssertionError("Google must not be queried"))
    # Persisted Google state is retained without making a deselected pipeline unhealthy.
    async with sessions.begin() as session:
        session.add(SyncState(data_type="steps", status="running", last_error="synthetic error"))

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    paths = set(application.openapi()["paths"])
    assert not any(
        path.startswith(
            (
                "/sync",
                "/backfill",
                "/google-health",
                "/auth/google",
                "/webhooks",
                "/api/webhooks",
                "/api/sync",
            )
        )
        for path in paths
    )
    assert ("/api/garmin/status" in paths) == settings.garmin_enabled
    assert ("/api/garmin/daily" in paths) == settings.garmin_enabled
    async with application.router.lifespan_context(application):
        jobs = {job.id for job in runtime.scheduler.get_jobs()}
        assert jobs == {"derived-refresh"} | (
            {"garmin-pull", "pipeline-watchdog"} if settings.garmin_enabled else set()
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application),
            base_url="http://localhost",
            headers={"Authorization": f"Bearer {settings.app_ui_token}"},
        ) as client:
            for endpoint in ("/health", "/api/status"):
                response = await client.get(endpoint)
                assert response.status_code == 200
                payload = response.json()
                assert payload["active_sources"] == settings.healthos_sources
                assert payload["configuration"] == "healthy"
                assert payload["status"] == ("degraded" if settings.garmin_enabled else "healthy")
                assert payload["watchdog"] == ("enabled" if settings.garmin_enabled else "disabled")
                assert (
                    "garmin_pull_uninitialized" in payload["reasons"]
                ) == settings.garmin_enabled
            assert (await client.post("/sync/all")).status_code == 404
            assert (await client.get("/api/devices")).json()["google"] == "not_queried"
            runtime.pipeline_health.inspect = AsyncMock(side_effect=OSError("synthetic outage"))
            response = await client.get("/api/status")
            assert response.status_code == 503
            assert response.json()["active_sources"] == settings.healthos_sources
    recover.assert_not_called()
    runtime.webhook_status.start.assert_not_called()
    assert runtime.watchdog.startup_check.call_count == int(settings.garmin_enabled)
    runtime.discovery.devices.assert_not_called()
    async with sessions() as session:
        row = await session.scalar(select(SyncState))
        assert row.status == "running" and row.last_error == "synthetic error"


def default_settings(monkeypatch) -> Settings:
    """The code's own defaults: no value leaks in from the environment CI or a shell sets."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    return Settings(_env_file=None)


async def test_default_google_jobs_and_routes_are_retained(monkeypatch, sessions):
    settings = default_settings(monkeypatch)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    application = main.create_app()
    runtime = application.state.runtime
    try:
        scheduler = build_scheduler(
            settings,
            runtime.reconciliation,
            runtime.watchdog,
            runtime.pipeline_health,
            runtime.identity,
            runtime.derived,
        )
        jobs = {job.id for job in scheduler.get_jobs()}
        assert jobs == {
            "daily-reconciliation",
            "weekly-reconciliation",
            "pipeline-watchdog",
            "pipeline-freshness-refresh",
            "derived-refresh",
        }
        assert {
            "/auth/google/start",
            "/sync/all",
            "/webhooks/google-health",
            "/api/webhooks/status",
            "/google-health/devices",
        } <= set(application.openapi()["paths"])
        assert not any(path.startswith("/api/garmin") for path in application.openapi()["paths"])
    finally:
        await runtime.shutdown()


def test_environment_verifier_honors_selection(tmp_path, key_file):
    path = tmp_path / "deployment.config"
    base = (
        "APP_UI_TOKEN=" + "s" * 32 + "\nPOSTGRES_PASSWORD=synthetic-pass\n"
        "DATABASE_URL=postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres/health\n"
        f"GARMIN_API_KEY_FILE={key_file}\n"
    )
    for selection in ("none", "garmin", "garmin,apple", "apple"):
        path.write_text(base + f"HEALTHOS_SOURCES={selection}\n")
        assert check_environment(path) == []
    path.write_text(base)
    assert any("GOOGLE_CLIENT_ID" in error for error in check_environment(path))
    path.write_text(base + "HEALTHOS_SOURCES=unknown\n")
    assert "HEALTHOS_SOURCES" in check_environment(path)[0]
    path.write_text(base + "HEALTHOS_SOURCES=garmin\nGARMIN_API_KEY_FILE=\n")
    assert check_environment(path) == ["GARMIN_API_KEY_FILE: missing, empty or unreadable"]
    key_file.unlink()
    path.write_text(base + "HEALTHOS_SOURCES=garmin\n")
    assert check_environment(path) == ["GARMIN_API_KEY_FILE: missing, empty or unreadable"]
