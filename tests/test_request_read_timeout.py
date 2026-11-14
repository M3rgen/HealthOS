"""HTTP session ownership, repeated transactions, exclusions and private timeout errors."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from support.backup_scripts import fake_age as fake_age
from support.backup_scripts import run
from support.backup_scripts import sandbox as sandbox
from support.postgres_reads import PLAN_CONTROL, READ_TIMEOUT_CONTROL, only_reads, read_statements

from app import main
from app.api.read_limits import request_session
from app.core.config import Settings
from app.db import session as database
from app.db.dialect import database as adapter


def request(settings=None, method="GET"):
    application = SimpleNamespace(
        state=SimpleNamespace(settings=settings or Settings(_env_file=None))
    )
    return Request({"type": "http", "method": method, "app": application})


@pytest.fixture
def controls(sessions, monkeypatch):
    implementation = adapter(sessions)
    original = implementation.control
    statements = []

    async def record(session, statement, parameters=None):
        statements.append(str(statement))
        if sessions.kw["bind"].dialect.name == "sqlite":
            # Exercise real reentrant I/O in the transaction hook even on SQLite.
            await session.execute(text("SELECT 1"))
        else:
            await original(session, statement, parameters)

    monkeypatch.setattr(implementation, "control", record)
    return statements


@pytest.fixture
async def application(sessions, monkeypatch):
    settings = Settings(_env_file=None, app_env="test", healthos_sources="google,garmin")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    app = main.create_app()
    yield app
    await app.state.runtime.shutdown()


@pytest.mark.parametrize("seconds", [-1, 2_147_484, 0.5, "wrong"])
def test_invalid_timeout_is_rejected_without_echoing_input(seconds):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, api_read_statement_timeout_seconds=seconds)
    assert "input_value" not in str(caught.value)


def test_timeout_default_and_environment(monkeypatch):
    monkeypatch.delenv("API_READ_STATEMENT_TIMEOUT_SECONDS", raising=False)
    assert Settings(_env_file=None).api_read_statement_timeout_seconds == 25
    monkeypatch.setenv("API_READ_STATEMENT_TIMEOUT_SECONDS", "0")
    assert Settings(_env_file=None).api_read_statement_timeout_seconds == 0


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_every_transaction_is_limited_but_savepoints_inherit(sessions, controls, method):
    async with request_session(sessions, request(method=method)) as session:
        assert await session.scalar(text("SELECT 1")) == 1
        async with session.begin_nested():
            assert await session.scalar(text("SELECT 2")) == 2
        await session.commit()
        assert await session.scalar(text("SELECT 3")) == 3
        await session.rollback()
        async with session.begin():
            assert await session.scalar(text("SELECT 4")) == 4
    assert controls == [READ_TIMEOUT_CONTROL] * 3
    # The listener is attached to that session only, not the shared factory or pool.
    async with sessions.begin() as session:
        assert await session.scalar(text("SELECT 5")) == 5
    assert len(controls) == 3


async def test_sync_orm_transaction_creation_defers_io_until_the_connection_begins(
    sessions, controls
):
    from datetime import UTC, datetime

    from app.models.manual import ManualMeasurement

    async with request_session(sessions, request()) as session:
        # Creating an ORM transaction has no greenlet yet. Controls belong to SQL I/O.
        session.add(ManualMeasurement(metric="weight", value=70, measured_at=datetime.now(UTC)))
        assert controls == []
        await session.flush()
        assert controls == [READ_TIMEOUT_CONTROL]
        await session.rollback()


@pytest.mark.parametrize("seconds,method", [(0, "GET"), (25, "POST"), (25, "DELETE")])
async def test_disabled_and_write_sessions_send_no_control(sessions, controls, seconds, method):
    settings = Settings(_env_file=None, api_read_statement_timeout_seconds=seconds)
    async with request_session(sessions, request(settings, method)) as session:
        await session.execute(text("SELECT 1"))
        await session.commit()
        await session.execute(text("SELECT 2"))
    assert controls == []


async def test_sqlite_adapter_sends_no_control_sql(sessions):
    if sessions.kw["bind"].dialect.name != "sqlite":
        return  # PostgreSQL's real control is covered by the transaction and integration tests.
    statements = []
    engine = sessions.kw["bind"].sync_engine

    def trace(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", trace)
    try:
        async with request_session(sessions, request()) as session:
            await session.execute(text("SELECT 1"))
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert statements == ["SELECT 1"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard?days=7",
        "/api/dashboard/history?metric=steps",
        "/api/dashboard/workouts",
        "/api/dashboard/measurements",
        "/api/dashboard/export.csv",
        "/api/sync/coverage?start_date=2026-11-01&end_date=2026-11-01&timezone=UTC",
        "/api/sync/reconciliation",
        "/api/devices",
        "/api/settings/appearance",
        "/api/settings/display",
        "/api/demo",
        "/sync/status",
        "/backfill/status",
        "/api/garmin/status",
        "/api/garmin/daily?from=2026-11-01&to=2026-11-01",
        "/api/webhooks/status",
        "/api/status",
        "/health",
    ],
)
async def test_http_read_routes_use_a_limited_session(application, controls, path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 200, response.text
    assert controls.count(READ_TIMEOUT_CONTROL) == 1
    assert set(controls) <= {READ_TIMEOUT_CONTROL, PLAN_CONTROL}


@pytest.mark.parametrize("code,status", [("57014", 504), ("40P01", 500), (None, 500)])
async def test_timeout_is_private_rolls_back_and_other_errors_are_not_relabelled(
    application, sessions, monkeypatch, caplog, code, status
):
    from app.api import dashboard

    observed = []

    async def fail(session, **kwargs):
        await session.execute(text("SELECT 1"))
        observed.append(session)
        session.rollback = AsyncMock(wraps=session.rollback)
        raise DBAPIError(
            "private query", {"value": "private value"}, SimpleNamespace(sqlstate=code)
        )

    monkeypatch.setattr(dashboard, "dashboard_data", fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application, raise_app_exceptions=False),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/api/dashboard")
    assert response.status_code == status
    assert "private" not in response.text
    events = [r for r in caplog.records if getattr(r, "event", None) == "API_READ_TIMEOUT"]
    assert len(events) == (code == "57014")
    assert "private" not in caplog.text
    if code == "57014":
        assert response.json() == {"detail": "Database read timed out. Please try again."}
        observed[0].rollback.assert_awaited_once()
        assert events[0].statement_timeout_seconds == 25
    assert not observed[0].in_transaction()
    async with sessions() as session:
        assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize(
    "error",
    [
        HTTPException(404),
        asyncio.CancelledError(),
        type("Raw", (Exception,), {"sqlstate": "40P01"})(),
    ],
)
async def test_errors_other_than_query_cancellation_pass_through_unchanged(sessions, error):
    with pytest.raises(BaseException) as raised:
        async with request_session(sessions, request()) as session:
            await session.execute(text("SELECT 1"))
            raise error
    assert raised.value is error


@pytest.mark.parametrize(
    "service,method,path",
    [
        ("garmin", "daily", "/api/garmin/daily?from=2026-11-01&to=2026-11-01"),
        ("webhook_status", "snapshot", "/api/webhooks/status"),
        ("pipeline_health", "inspect", "/api/status"),
    ],
)
async def test_service_owned_read_timeouts_keep_the_same_safe_contract(
    application, monkeypatch, service, method, path
):
    async def fail(*args, session, **kwargs):
        await session.execute(text("SELECT 1"))
        raise DBAPIError("private query", {}, SimpleNamespace(sqlstate="57014"))

    monkeypatch.setattr(getattr(application.state.runtime, service), method, fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 504
    assert response.json() == {"detail": "Database read timed out. Please try again."}


async def test_health_reports_a_read_timeout_as_an_unavailable_database(
    application, monkeypatch, caplog
):
    async def fail(*args, session, **kwargs):
        await session.execute(text("SELECT 1"))
        raise DBAPIError("private query", {}, SimpleNamespace(sqlstate="57014"))

    monkeypatch.setattr(application.state.runtime.pipeline_health, "inspect", fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        health = await client.get("/health")
        status = await client.get("/api/status")
    # Monitors and the container healthcheck keep the 503 report; the interface keeps the 504.
    assert health.status_code == 503
    body = health.json()
    assert {key: body[key] for key in ("status", "database", "service")} == {
        "status": "failed",
        "database": "unavailable",
        "service": "healthos",
    }
    assert {"active_sources", "scheduler", "configuration", "ui_login_disabled"} < body.keys()
    assert status.status_code == 504
    assert status.json() == {"detail": "Database read timed out. Please try again."}
    assert "private" not in health.text + caplog.text
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count("API_READ_TIMEOUT") == 2
    unavailable = [
        r for r in caplog.records if getattr(r, "event", None) == "HEALTH_DATABASE_UNAVAILABLE"
    ]
    assert [r.error_type for r in unavailable] == ["read_timeout"]


async def test_dependency_session_is_limited_only_inside_an_http_read(
    application, sessions, controls
):
    from app.db.request_sessions import read_limit

    # Outside a request (and after one) the database layer's dependency sends no control.
    async for session in database.get_session():
        await session.execute(text("SELECT 1"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/dashboard/measurements")).status_code == 200
    assert controls == [READ_TIMEOUT_CONTROL]
    assert read_limit.get() == 0
    async for session in database.get_session():
        await session.execute(text("SELECT 2"))
    assert controls == [READ_TIMEOUT_CONTROL]


async def test_service_reads_remain_unlimited_outside_http(application, controls):
    runtime = application.state.runtime
    await runtime.garmin.status()
    await runtime.garmin.daily(date(2026, 11, 1), date(2026, 11, 1))
    await runtime.webhook_status.snapshot()
    await runtime.pipeline_health.inspect()
    assert controls == []


@pytest.mark.parametrize(
    "worker",
    "incremental backfill garmin reconciliation identity webhook_processor".split(),
)
async def test_workers_keep_the_unmodified_factory_even_during_a_read(
    application, sessions, controls, worker
):
    import asyncio

    factory = getattr(application.state.runtime, worker).sessions
    assert factory is sessions

    async def work():
        async with factory.begin() as session:
            assert await session.scalar(text("SELECT 1")) == 1

    async with request_session(sessions, request()) as session:
        await session.execute(text("SELECT 1"))
        await asyncio.create_task(work())
    assert controls == [READ_TIMEOUT_CONTROL]


async def test_fit_http_import_is_unlimited(application, controls):
    from fit_factory import activity

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/imports/garmin-fit",
            content=activity(),
            headers={"Content-Type": "application/octet-stream", "X-HealthOS-Request": "1"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] > 0
    assert READ_TIMEOUT_CONTROL not in controls


async def test_webhook_ingestion_is_unlimited(application, controls, monkeypatch):
    runtime = application.state.runtime
    application.state.settings.google_health_webhook_authorization = "synthetic-test"
    monkeypatch.setattr(runtime.webhook_verifier, "verify", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/webhooks/google-health",
            content=b"[]",
            headers={"Authorization": "synthetic-test", "GOOGLE-HEALTH-API-SIGNATURE": "synthetic"},
        )
    assert response.status_code == 204
    assert READ_TIMEOUT_CONTROL not in controls


def test_apple_importer_is_not_installed_and_backup_does_not_receive_the_setting():
    from pathlib import Path

    import yaml

    from app.core.sources import not_installed_sources

    # GitHub-only Apple extension: this tree ships the importer (app/apple/).
    assert not_installed_sources(["apple"]) == []
    services = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]
    assert "API_READ_STATEMENT_TIMEOUT_SECONDS" in services["app"]["environment"]
    assert "API_READ_STATEMENT_TIMEOUT_SECONDS" not in services["backup"]["environment"]


def test_backup_process_keeps_its_own_unlimited_database_path(fake_age):
    from pathlib import Path

    result = run("backup.sh", {**fake_age, "API_READ_STATEMENT_TIMEOUT_SECONDS": "1"})
    assert result.returncode == 0, result.stderr
    commands = Path(fake_age["FAKE_DOCKER_LOG"]).read_text()
    assert "--profile backup run --rm -T backup" in commands
    assert "statement_timeout" not in commands
    assert "PGOPTIONS" not in commands


@pytest.mark.parametrize(
    "statement",
    [
        "SET statement_timeout = 25000",
        "SET LOCAL statement_timeout = 0",
        "SET LOCAL statement_timeout = 1000",
        "SET LOCAL statement_timeout = 25000; SELECT 1",
        "SET LOCAL plan_cache_mode = auto",
        "SET LOCAL lock_timeout = 25000",
        "UPDATE raw_health_data",
    ],
)
def test_read_assertions_do_not_allow_other_controls(statement):
    assert read_statements([PLAN_CONTROL, READ_TIMEOUT_CONTROL, "SELECT 1"]) == ["SELECT 1"]
    assert read_statements([statement]) == [statement]
    with pytest.raises(AssertionError):
        only_reads(None, None, statement, None, None, False)
