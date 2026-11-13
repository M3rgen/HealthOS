"""A selected source whose importer is absent says so, and only then (synthetic data only)."""

import logging
from datetime import date, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app import main
from app.core import sources as source_rules
from app.core.config import Settings

PRODUCTION = {
    "app_env": "production",
    # make test-postgres connects as the disposable server's superuser, which production
    # refuses without this dated opt-in (app/core/role_policy.py).
    "healthos_allow_superuser_until": date.today() + timedelta(days=1),
    "database_url": "postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health",
    "app_ui_token": "synthetic-importer-ui-token-varied-characters",
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
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    "selection, installed, reasons",
    [
        ("apple", False, ["apple_importer_not_installed"]),
        ("garmin,apple", False, ["apple_importer_not_installed"]),
        ("apple", True, []),
        ("garmin", False, []),
        ("none", False, []),
    ],
)
async def test_selected_source_without_its_importer_is_named(
    selection, installed, reasons, key_file, sessions, monkeypatch, caplog
):
    settings = Settings(
        _env_file=None, healthos_sources=selection, garmin_api_key_file=key_file, **PRODUCTION
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    spec = object() if installed else None
    monkeypatch.setattr(source_rules, "find_spec", lambda name: spec)
    application = main.create_app()
    runtime = application.state.runtime
    runtime.watchdog.startup_check = AsyncMock()
    logging.getLogger().addHandler(caplog.handler)  # create_app() replaced the root handlers
    with caplog.at_level(logging.WARNING, logger="app.main"):
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(application),
                base_url="http://localhost",
                headers={"Authorization": f"Bearer {settings.app_ui_token}"},
            ) as client:
                payload = (await client.get("/health")).json()
    # On a superuser test database the role reason (tests/test_security_hardening.py) joins these.
    expected = (["garmin_pull_uninitialized"] if settings.garmin_enabled else []) + reasons
    assert [
        item for item in payload["reasons"] if not item.startswith("database_role_")
    ] == expected
    assert payload["status"] == ("degraded" if settings.garmin_enabled else "healthy")
    assert payload["active_sources"] == settings.healthos_sources
    warned = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "SOURCE_IMPORTER_NOT_INSTALLED"
    ]
    assert bool(warned) == bool(reasons) and all(record.source == "apple" for record in warned)
