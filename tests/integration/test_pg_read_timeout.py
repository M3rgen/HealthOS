"""A browser abandoning a read must leave at most one bounded PostgreSQL statement."""

import asyncio
import json
import logging
import time

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.api import dashboard
from app.api.read_limits import request_session
from app.core.config import Settings
from app.db import request_sessions
from app.db import session as database

pytestmark = pytest.mark.integration
SLOW_READ = "SELECT pg_sleep(3) /* browser_read_timeout_probe */"
# Only the fifteenth fetch is slow: the driver raises its own error, unwrapped.
STREAMED_READ = (
    "SELECT g, pg_sleep(CASE WHEN g = 1500 THEN 3 ELSE 0 END) "
    "FROM generate_series(1, 2000) AS g /* browser_read_timeout_probe */"
)
# Only a hang guard: the outcome is proven by the server's own events, never by elapsed time.
HANG_GUARD_SECONDS = 30
TIMEOUT_REASON = "canceling statement due to statement timeout"


def cancellation(error):
    """The server's SQLSTATE and reason, whether SQLAlchemy wrapped the driver error or not."""
    original = getattr(error, "orig", error)
    return getattr(original, "sqlstate", None), str(original)


def statement_timeouts(errors):
    """Count the reads PostgreSQL itself cancelled for exceeding statement_timeout."""
    return sum(
        code == "57014" and TIMEOUT_REASON in reason for code, reason in map(cancellation, errors)
    )


@pytest.fixture
async def slow_dashboard(database_url, migrated, monkeypatch, request):
    query = getattr(request, "param", SLOW_READ)
    # Reuse exactly one physical connection after the failed request.
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        await session.execute(text("CREATE TEMP TABLE read_timeout_rollback_probe (value int)"))
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(main, "SessionLocal", factory)
    settings = Settings(_env_file=None, app_env="test", healthos_sources="google,garmin")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    original = dashboard.dashboard_data
    started = asyncio.Event()
    backend = []
    # Counted events of the slow read: the limit it ran under, how it failed, whether it finished.
    reads = {"limits": [], "errors": [], "finished": 0}

    async def delayed(session, **kwargs):
        backend.append(await session.scalar(text("SELECT pg_backend_pid()")))
        reads["limits"].append(await session.scalar(text("SHOW statement_timeout")))
        await session.execute(text("INSERT INTO read_timeout_rollback_probe VALUES (1)"))
        started.set()
        try:
            if query == SLOW_READ:
                await session.execute(text(query))
            else:
                options = {"yield_per": 100}
                async for _ in await session.stream(text(query), execution_options=options):
                    pass
        except Exception as error:
            reads["errors"].append(error)
            raise
        reads["finished"] += 1
        return await original(session, **kwargs)

    monkeypatch.setattr(dashboard, "dashboard_data", delayed)
    application = main.create_app()
    try:
        yield application, settings, started, backend, factory, original, reads
    finally:
        await application.state.runtime.shutdown()
        await engine.dispose()


def browser_request(application):
    """An ASGI browser connection: disconnecting does not cancel the server task."""
    disconnected = asyncio.Event()
    messages = []

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/dashboard",
        "raw_path": b"/api/dashboard",
        "query_string": b"days=7",
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 12345),
    }
    return asyncio.create_task(application(scope, receive, send)), disconnected, messages


async def active_probe(sessions, pid, query=SLOW_READ):
    async with sessions() as observer:
        return await observer.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE pid = :pid AND state = 'active' AND query = :query"
            ),
            {"pid": pid, "query": query},
        )


async def wait_for_probe(sessions, pid, query=SLOW_READ):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if await active_probe(sessions, pid, query):
            return
        await asyncio.sleep(0.01)
    pytest.fail("the artificial slow read never became active")


async def test_abandoned_browser_keeps_running_without_a_server_limit(slow_dashboard, pg_sessions):
    application, settings, started, backend, _, _, reads = slow_dashboard
    # This reproduces the pre-fix behavior and keeps the explicit off switch covered.
    settings.api_read_statement_timeout_seconds = 0
    task, disconnected, messages = browser_request(application)
    try:
        await asyncio.wait_for(started.wait(), 2)
        await wait_for_probe(pg_sessions, backend[0])
        disconnected.set()
        await asyncio.sleep(0.1)
        assert not task.done()
        assert await active_probe(pg_sessions, backend[0]) == 1
        await asyncio.wait_for(task, HANG_GUARD_SECONDS)
        assert messages[0]["status"] == 200
        assert reads == {"limits": ["0"], "errors": [], "finished": 1}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize(
    "slow_dashboard,query",
    [(SLOW_READ,) * 2, (STREAMED_READ,) * 2],
    ids=["execute", "stream"],
    indirect=["slow_dashboard"],
)
async def test_slow_dashboard_times_out_rolls_back_and_reuses_connection(
    slow_dashboard, pg_sessions, monkeypatch, caplog, disconnect, query
):
    application, settings, started, backend, factory, original, reads = slow_dashboard
    settings.api_read_statement_timeout_seconds = 1
    # The migration's fileConfig() disabled the application's existing loggers.
    monkeypatch.setattr(logging.getLogger(request_sessions.__name__), "disabled", False)
    task, disconnected, messages = browser_request(application)
    try:
        await asyncio.wait_for(started.wait(), 2)
        await wait_for_probe(pg_sessions, backend[0], query)
        if disconnect:
            disconnected.set()
        await asyncio.wait_for(task, HANG_GUARD_SECONDS)
        # The 3 s read ran under the 1 s limit and PostgreSQL cancelled it for that limit:
        # not a client cancellation, not a completed read relabelled as a timeout.
        assert reads["limits"] == ["1s"]
        assert (len(reads["errors"]), statement_timeouts(reads["errors"])) == (1, 1)
        assert reads["finished"] == 0
        assert messages[0]["status"] == 504
        body = b"".join(item.get("body", b"") for item in messages[1:])
        assert json.loads(body) == {"detail": "Database read timed out. Please try again."}
        events = [r for r in caplog.records if getattr(r, "event", None) == "API_READ_TIMEOUT"]
        assert [r.statement_timeout_seconds for r in events] == [1]
        assert await active_probe(pg_sessions, backend[0], query) == 0
        async with factory() as session:
            assert await session.scalar(text("SELECT pg_backend_pid()")) == backend[0]
            assert await session.scalar(text("SHOW statement_timeout")) == "0"
            assert (
                await session.scalar(text("SELECT count(*) FROM read_timeout_rollback_probe")) == 0
            )
        monkeypatch.setattr(dashboard, "dashboard_data", original)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://localhost"
        ) as client:
            assert (await client.get("/api/dashboard?days=7")).status_code == 200
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_slow_health_read_keeps_the_monitor_503(slow_dashboard, monkeypatch, caplog):
    from app.api import health

    application, settings, _, _, _, _, _ = slow_dashboard
    settings.api_read_statement_timeout_seconds = 1
    monkeypatch.setattr(logging.getLogger(request_sessions.__name__), "disabled", False)
    monkeypatch.setattr(logging.getLogger(health.__name__), "disabled", False)
    errors = []

    async def slow(*args, session, **kwargs):
        try:
            await session.execute(text(SLOW_READ))
        except Exception as error:
            errors.append(error)
            raise

    monkeypatch.setattr(application.state.runtime.pipeline_health, "inspect", slow)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost"
    ) as client:
        response = await asyncio.wait_for(client.get("/health"), HANG_GUARD_SECONDS)
        assert statement_timeouts(errors) == 1
        status = await asyncio.wait_for(client.get("/api/status"), HANG_GUARD_SECONDS)
        assert status.status_code == 504
    # Both reads were cancelled by the server's statement_timeout, nothing else.
    assert (len(errors), statement_timeouts(errors)) == (2, 2)
    assert response.status_code == 503
    assert response.json()["status"] == "failed"
    assert response.json()["database"] == "unavailable"
    events = [r for r in caplog.records if getattr(r, "event", None) == "API_READ_TIMEOUT"]
    assert [r.statement_timeout_seconds for r in events] == [1, 1]


@pytest.mark.parametrize(
    "path",
    "incremental backfill garmin reconciliation identity webhook_processor cli".split(),
)
async def test_background_and_cli_sessions_do_not_cancel_the_same_long_read(slow_dashboard, path):
    from app.cli._context import CommandContext

    application, settings, _, _, factory, _, _ = slow_dashboard
    settings.api_read_statement_timeout_seconds = 1
    runtime = application.state.runtime
    if path == "cli":
        sessions = CommandContext(
            session_factory=lambda: factory, settings=lambda: settings
        ).sessions
    else:
        sessions = getattr(runtime, path).sessions
    began = time.monotonic()
    async with sessions() as session:
        assert await session.scalar(text("SHOW statement_timeout")) == "0"
        await session.execute(text(SLOW_READ))
        assert await session.scalar(text("SELECT 1")) == 1
    assert time.monotonic() - began >= 2.8


@pytest.mark.parametrize("seconds", [0, 1, 25])
async def test_timeout_control_is_transaction_local_and_zero_sends_nothing(slow_dashboard, seconds):
    from fastapi import Request
    from sqlalchemy import event

    application, settings, _, _, factory, _, _ = slow_dashboard
    settings.api_read_statement_timeout_seconds = seconds
    statements = []
    engine = factory.kw["bind"].sync_engine

    def trace(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", trace)
    try:
        async with request_session(
            factory,
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "app": application,
                }
            ),
        ) as session:
            for finish in (session.commit, session.rollback, session.commit):
                assert await session.scalar(text("SHOW statement_timeout")) == (
                    f"{seconds}s" if seconds else "0"
                )
                async with session.begin_nested():
                    assert await session.scalar(text("SELECT 1")) == 1
                await finish()
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    controls = [sql for sql in statements if sql.startswith("SET")]
    assert controls == ([f"SET LOCAL statement_timeout = {seconds * 1000}"] * 3 if seconds else [])
    async with factory() as session:
        assert await session.scalar(text("SHOW statement_timeout")) == "0"


def test_migration_connection_does_not_cancel_the_same_long_read(alembic_config, monkeypatch):
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    from app.core.config import get_settings

    monkeypatch.setenv("API_READ_STATEMENT_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    original = MigrationContext.run_migrations

    def run(context, **kwargs):
        assert context.connection.scalar(text("SHOW statement_timeout")) == "0"
        context.connection.execute(text(SLOW_READ))
        return original(context, **kwargs)

    monkeypatch.setattr(MigrationContext, "run_migrations", run)
    began = time.monotonic()
    command.upgrade(alembic_config, "head")
    assert time.monotonic() - began >= 2.8
