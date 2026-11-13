from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.logging import RedactSecrets
from app.db.base import Base

# Helpers in tests/support/ assert too: rewrite them like test modules, before any imports them.
pytest.register_assert_rewrite("support")

from support.sqlite_database import install  # noqa: E402

install()


def pytest_collection_modifyitems(config, items):
    from support import pg_backend

    if pg_backend.BACKEND == "postgres":
        items[:], shaped = pg_backend.split_sqlite_shaped(items)
        config.hook.pytest_deselected(items=shaped)


# A developer .env with APP_UI_TOKEN must not put the shared test app behind the login;
# auth tests pass the token explicitly.
os.environ["APP_UI_TOKEN"] = ""


# Production's default level (Settings.log_level). Importing app.main runs create_app(),
# whose configure_logging() would otherwise set the root level only when that module is
# collected: the same test would see INFO in the full suite and WARNING when run alone.
ROOT_TEST_LEVEL = logging.INFO
QUIET_TRANSPORT_LOGGERS = ("httpx", "httpcore")


@pytest.fixture(autouse=True)
def preserved_umask() -> Iterator[None]:
    """Keep process-wide permission changes from leaking into the next test."""
    previous = os.umask(0o022)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


def _baseline_root() -> None:
    root = logging.getLogger()
    root.setLevel(ROOT_TEST_LEVEL)
    # Drop configure_logging()'s stdout handler; pytest's capture handlers stay.
    root.handlers = [
        handler
        for handler in root.handlers
        if not any(isinstance(item, RedactSecrets) for item in handler.filters)
    ]
    for name in QUIET_TRANSPORT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


@pytest.fixture(autouse=True)
def preserved_loggers() -> Iterator[None]:
    """Start every test from one logging baseline and undo its changes afterwards.

    migrations/env.py runs logging.config.fileConfig(), which disables every existing
    logger and replaces the root handlers; create_app() resets the root level and
    handlers. Later caplog tests would then see no records or a different level.
    Snapshotting costs microseconds, so every test (unit or integration) is covered.
    """
    _baseline_root()
    manager = logging.Logger.manager
    loggers = [logging.getLogger()] + [
        item for item in manager.loggerDict.values() if isinstance(item, logging.Logger)
    ]
    saved = [
        (item, item.disabled, item.level, list(item.handlers), item.propagate) for item in loggers
    ]
    try:
        yield
    finally:
        for item, disabled, level, handlers, propagate in saved:
            item.disabled, item.level, item.propagate = disabled, level, propagate
            item.handlers = handlers
        # Loggers first created during the migration (alembic.*) are re-enabled too.
        for item in list(manager.loggerDict.values()):
            if isinstance(item, logging.Logger) and item not in loggers:
                item.disabled = False


_schema_image: bytes | None = None


def _sqlite_schema_image() -> bytes:
    """The empty schema, built once per process: create_all took 130+ ms per test (triggers and
    views included), and this suite opens several thousand databases."""
    global _schema_image
    if _schema_image is None:
        engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(engine)
        _schema_image = engine.raw_connection().driver_connection.serialize()
        engine.dispose()
    return _schema_image


@pytest_asyncio.fixture
async def sessions():
    from support import pg_backend

    if pg_backend.BACKEND == "postgres":
        async with pg_backend.postgres_sessions() as factory:
            yield factory
        return
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as connection:
        # One pooled connection holds the in-memory database, so loading it here is for good.
        raw = (await connection.get_raw_connection()).driver_connection
        await raw._execute(raw._conn.deserialize, _sqlite_schema_image())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def unlocked_pull():
    """Opt in to Garmin ingestion without a run lock using the shared test factory."""
    from support.garmin import unlocked

    return unlocked


@pytest.fixture
def clock(monkeypatch):
    """store_records' wall clock and the database clock of the sightings (identity tests:
    sightings decide what was seen together)."""
    from identity_records import Clock

    from app.services.archive import storage
    from app.services.identities import source_chain_repository

    current = Clock()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - signature of datetime.now
            return current.now

    async def database_now(session):  # noqa: ARG001 - signature of the real one
        return current.now

    monkeypatch.setattr(storage, "datetime", Frozen)
    monkeypatch.setattr(source_chain_repository, "database_now", database_now)
    return current


@pytest.fixture
def local_sockets() -> None:
    """A real loopback listener is needed: see support/sockets.py for when this skips."""
    from support.sockets import require_local_bind

    require_local_bind()


@pytest.fixture(scope="module")
def module_local_sockets() -> None:
    """The same bind requirement for module-scoped runtime probes."""
    from support.sockets import require_local_bind

    require_local_bind()


@pytest.fixture
def local_unix_sockets() -> None:
    """Unix transport tests may run even when TCP listeners are forbidden."""
    import socket

    from support.sockets import require_local_bind

    # Short path: macOS limits Unix socket paths to 104 bytes.
    with tempfile.TemporaryDirectory(prefix="bind-") as directory:
        require_local_bind(family=socket.AF_UNIX, address=os.path.join(directory, "s"))


@pytest.fixture
def expiring_streams(monkeypatch):
    """Google response streams run their budgets out when they stall (support/loop_clock.py).

    Budgets keep their production lengths: on a loaded machine a 20 ms budget also cut off
    the healthy reads around the stall and the test reported a timeout that never happened."""
    from raw_response_records import PausedBody

    monkeypatch.setattr(PausedBody, "expire_budgets", True)


@pytest.fixture
def frozen_now(monkeypatch):
    """The application's wall clock stands still at the test's start (support/clock.py)."""
    from datetime import UTC

    from support.clock import freeze_app_clock

    return freeze_app_clock(monkeypatch, datetime.now(UTC))


@pytest_asyncio.fixture
async def dashboard_dates(sessions, monkeypatch):
    """Production routes, boundary archive, SQL trace and a read-only snapshot."""
    from datetime import UTC, date, timedelta

    import httpx
    from derived_records import manual, sleep, snapshot, steps, store, summary
    from fastapi import FastAPI
    from sqlalchemy import event
    from support.workouts import exercise

    import app.api.dashboard as api
    import app.services.dashboard_views.dashboard as service
    from app.db.session import get_session
    from app.models.manual import ManualWorkout

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(9999, 12, 31, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(api, "datetime", Fixed)
    monkeypatch.setattr(service, "datetime", Fixed)
    start = datetime(1, 1, 1, tzinfo=UTC)
    await store(sessions, "steps", steps(start=start, count=120))
    await store(sessions, "steps", steps(start=start, count=240))
    await store(sessions, "sleep", sleep(start=start, end=start + timedelta(hours=1)))
    await store(
        sessions,
        "daily-resting-heart-rate",
        summary("daily-resting-heart-rate", day=date.min, beatsPerMinute=71),
    )
    await store(sessions, "exercise", exercise(start=start))
    await manual(sessions, 70)
    async with sessions.begin() as session:
        session.add(
            ManualWorkout(
                exercise_type="Yürüyüş", start_time=start, end_time=start + timedelta(hours=1)
            )
        )
    async with sessions() as session:
        before = await snapshot(session)

    application = FastAPI()
    application.include_router(api.router)

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    statements = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", trace)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        yield client, statements
    event.remove(engine, "before_cursor_execute", trace)
    async with sessions() as session:
        assert await snapshot(session) == before
