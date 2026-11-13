"""Production refuses a superuser database login; a dated opt-in bridges an upgrade."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.auth import build

from app import main
from app.api import health as health_api
from app.core.config import Settings
from app.core.role_policy import (
    GRACE_LIMIT,
    SuperuserRuntimeRefused,
    superuser_grace,
)
from app.db import role_check

TODAY = date(2026, 11, 11)


def test_no_opt_in_names_the_exact_provisioning_step():
    with pytest.raises(SuperuserRuntimeRefused) as refused:
        superuser_grace(None, TODAY)
    message = str(refused.value)
    assert "POSTGRES_APP_USER=healthos_app" in message
    assert "POSTGRES_APP_PASSWORD" in message and "docker compose up -d" in message
    assert "provision-app-role" in message and "HEALTHOS_ALLOW_SUPERUSER_UNTIL" in message


@pytest.mark.parametrize(
    "until, accepted",
    [
        (TODAY, True),
        (TODAY + timedelta(days=1), True),
        (TODAY + GRACE_LIMIT, True),
        (TODAY + GRACE_LIMIT + timedelta(days=1), False),
        (TODAY - timedelta(days=1), False),
        (date(2099, 1, 1), False),
    ],
)
def test_the_opt_in_is_bounded(until, accepted):
    if accepted:
        assert superuser_grace(until, TODAY) == until
        return
    with pytest.raises(SuperuserRuntimeRefused, match="provision-app-role"):
        superuser_grace(until, TODAY)


@pytest.mark.parametrize(
    "value, expected", [("", None), ("  ", None), ("2026-11-20", date(2026, 11, 20))]
)
def test_setting_reads_an_iso_date_and_treats_empty_as_unset(value, expected):
    settings = Settings(_env_file=None, healthos_allow_superuser_until=value)
    assert settings.healthos_allow_superuser_until == expected


def _settings(until=None):
    return SimpleNamespace(healthos_allow_superuser_until=until)


def test_gate_refuses_and_logs_the_step(caplog):
    state = SimpleNamespace(database_role_risk=role_check.SUPERUSER)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        with pytest.raises(SuperuserRuntimeRefused):
            main.gate_database_role(state, _settings(), today=TODAY)
    record = next(r for r in caplog.records if r.event == "DATABASE_ROLE_SUPERUSER_REFUSED")
    assert "POSTGRES_APP_USER" in record.getMessage()
    assert not hasattr(state, "database_role_grace_until")


def test_gate_accepts_a_current_opt_in_and_remembers_it(caplog):
    state = SimpleNamespace(database_role_risk=role_check.SUPERUSER)
    until = TODAY + timedelta(days=3)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        main.gate_database_role(state, _settings(until), today=TODAY)
    assert state.database_role_grace_until == until
    assert any(r.event == "DATABASE_ROLE_SUPERUSER_GRACE" for r in caplog.records)


@pytest.mark.parametrize("risk", [role_check.SCHEMA_OWNER, None])
def test_gate_leaves_other_roles_running(risk):
    state = SimpleNamespace(database_role_risk=risk)
    main.gate_database_role(state, _settings(), today=TODAY)
    assert not hasattr(state, "database_role_grace_until")


class FakeSession:
    kw = {"bind": object()}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, _statement):
        return None


@pytest.mark.parametrize("risk", [role_check.SUPERUSER, role_check.PRIVILEGED_ROLE])
async def test_production_lifespan_does_not_start_as_superuser(monkeypatch, risk):
    application = build(monkeypatch)
    application.state.settings.app_env = "production"
    application.state.settings.healthos_sources = ["none"]
    monkeypatch.setattr(main, "database_role_risk", AsyncMock(return_value=risk))
    monkeypatch.setattr(main, "SessionLocal", FakeSession)
    application.state.runtime.scheduler_needed = lambda _settings: False
    application.state.runtime.shutdown = AsyncMock()
    with pytest.raises(SuperuserRuntimeRefused):
        async with application.router.lifespan_context(application):
            pytest.fail("the application must not become ready")
    application.state.runtime.shutdown.assert_awaited()


@pytest.mark.parametrize("risk", [role_check.SUPERUSER, role_check.PRIVILEGED_ROLE])
@pytest.mark.parametrize("grace", [False, True])
async def test_health_says_the_superuser_runs_on_its_grace(risk, grace):
    settings = SimpleNamespace(
        production=True,
        healthos_sources=["none"],
        google_enabled=False,
        garmin_enabled=False,
        app_ui_token="x",
        watchdog_enabled=False,
        backup_status_file=None,
    )
    snapshot = SimpleNamespace(as_dict=lambda **_: {"status": "healthy", "reasons": []})
    runtime = SimpleNamespace(
        scheduler_needed=lambda _settings: False,
        scheduler_running=False,
        pipeline_health=SimpleNamespace(inspect=AsyncMock(return_value=snapshot)),
    )
    state = SimpleNamespace(
        runtime=runtime,
        settings=settings,
        database_role_risk=risk,
        database_role_grace_until=datetime.now(UTC).date() if grace else None,
    )
    payload = await health_api.health(SimpleNamespace(app=SimpleNamespace(state=state)))
    expected = [f"database_role_{risk}"]
    if grace:
        expected.append(f"database_role_{risk}_grace")
    assert payload["reasons"] == expected
    assert payload["status"] == "healthy"


@pytest.mark.parametrize(
    "until, accepted",
    [
        (None, False),
        (TODAY - timedelta(days=1), False),
        (TODAY, True),
        (TODAY + GRACE_LIMIT, True),
        (TODAY + GRACE_LIMIT + timedelta(days=1), False),
    ],
)
def test_privileged_gate_refuses_or_uses_the_same_bounded_grace(until, accepted, caplog):
    state = SimpleNamespace(database_role_risk=role_check.PRIVILEGED_ROLE)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        if accepted:
            main.gate_database_role(state, _settings(until), today=TODAY)
            assert state.database_role_grace_until == until
            assert any(r.event == "DATABASE_ROLE_PRIVILEGED_GRACE" for r in caplog.records)
        else:
            with pytest.raises(SuperuserRuntimeRefused) as refused:
                main.gate_database_role(state, _settings(until), today=TODAY)
            message = str(refused.value)
            assert "privileged database login" in message
            assert "REVOKE <role> FROM <login>;" in message
            assert "NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS" in message
            assert "docker compose up -d" in message
            assert any(r.event == "DATABASE_ROLE_PRIVILEGED_REFUSED" for r in caplog.records)
            assert not hasattr(state, "database_role_grace_until")
