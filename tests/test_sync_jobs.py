"""Manual sync admission and bounded pool evidence with synthetic Google responses."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import dashboard, sync
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.db import engine as engine_module
from app.db.base import Base
from app.db.session import get_session
from app.google_health.client import Page
from app.runtime import build_runtime
from app.services.synchronization.sync_jobs import SyncJobs
from app.sync.ownership import (
    NOTIFICATION_NAMESPACE,
    RUN_NAMESPACE,
    WATCHDOG_NAMESPACE,
    ownership_connection,
)

HEADERS = {"X-HealthOS-Request": "1"}


@asynccontextmanager
async def sync_application(sessions):
    settings = Settings(_env_file=None, app_env="test", enabled_data_types=["steps"])
    runtime = build_runtime(settings, sessions)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(*args, **kwargs):
        # OAuth and response archiving must be able to use the same pool while
        # PostgreSQL ownership keeps its exact connection checked out.
        async with sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        entered.set()
        await release.wait()
        return Page([], None)

    runtime.incremental.source.client = SimpleNamespace(
        list_data_points=AsyncMock(side_effect=fetch)
    )
    application = FastAPI()
    application.state.settings = settings
    application.state.runtime = runtime
    application.state.write_limits = WriteLimits(settings)
    application.include_router(sync.router)
    application.include_router(dashboard.router)

    async def session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://localhost"
        ) as client:
            yield client, runtime, entered, release
    finally:
        await runtime.shutdown()


async def assert_concurrent_sync(sessions, path):
    """Also executed against PostgreSQL by tests/integration/test_pg_sync_jobs.py."""
    pool = sessions.kw["bind"].pool
    peak = 0

    def checkout(*args):
        nonlocal peak
        peak = max(peak, pool.checkedout())

    event.listen(pool, "checkout", checkout)
    try:
        async with sync_application(sessions) as (client, runtime, entered, release):
            responses = await asyncio.gather(
                *(client.post(path, headers=HEADERS) for _ in range(20))
            )
            await asyncio.wait_for(entered.wait(), 30)
            assert all(response.status_code == 202 for response in responses)
            assert len({r.json()["job_id"] for r in responses}) == 1
            assert len(runtime.tasks) == len(runtime.sync_jobs.active) == 1
            assert runtime.incremental.source.client.list_data_points.await_count == 1
            assert 1 <= peak <= 2
            assert pool.checkedout() <= 1
            if path == "/sync/all":
                assert {r.json()["sequence"] for r in responses} == {0}
                runtime.incremental.sequence = 7
                assert (await client.post(path, headers=HEADERS)).json() == responses[0].json()
            task = next(iter(runtime.tasks))
            release.set()
            await asyncio.wait_for(task, 30)
            # After completion a new manual request starts a fresh job on the same pool.
            next_response = await client.post(path, headers=HEADERS)
            assert next_response.json()["job_id"] != responses[0].json()["job_id"]
        assert pool.checkedout() == 0
    finally:
        event.remove(pool, "checkout", checkout)


@pytest.mark.parametrize("path", ["/sync/all", "/sync/steps"])
async def test_twenty_requests_share_one_job_and_one_bounded_engine(tmp_path, monkeypatch, path):
    create = Mock(wraps=engine_module.create_async_engine)
    monkeypatch.setattr(engine_module, "create_async_engine", create)
    engine = engine_module.create_shared_engine(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}",
            sync_concurrency=2,
        )
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await assert_concurrent_sync(async_sessionmaker(engine, expire_on_commit=False), path)
        assert create.call_count == 1
        assert engine.pool.size() == 9 and engine.pool._max_overflow == 0
    finally:
        await engine.dispose()


def test_postgres_pool_has_fixed_capacity_without_connecting():
    engine = engine_module.create_shared_engine(
        Settings(_env_file=None, database_url="postgresql+asyncpg://unused", sync_concurrency=2)
    )
    assert engine.pool.size() == 9 and engine.pool._max_overflow == 0
    assert engine.get_execution_options()["ownership_concurrency"] == 2


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled"])
async def test_finished_jobs_release_scope_and_other_scopes_stay_independent(ending):
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(*args):
        entered.set()
        await release.wait()
        if ending == "failed":
            raise RuntimeError("synthetic failure")

    incremental = SimpleNamespace(sequence=3, run=run, run_all=run)
    jobs = SyncJobs()
    job = jobs.start("steps", incremental, asyncio.create_task)
    await entered.wait()
    assert jobs.start("steps", incremental, asyncio.create_task) is job
    other = jobs.start("heart-rate", incremental, asyncio.create_task)
    assert other.job_id != job.job_id
    if ending == "cancelled":
        job.task.cancel()
    release.set()
    await asyncio.gather(job.task, other.task, return_exceptions=True)
    assert jobs.active == {}
    next_job = jobs.start("steps", incremental, asyncio.create_task)
    assert next_job.job_id != job.job_id
    await asyncio.gather(next_job.task, return_exceptions=True)


async def test_sync_post_budget_is_shared_and_gets_bypass_it(sessions):
    async with sync_application(sessions) as (client, runtime, entered, release):
        # Consume a slot through an existing manual-write route before sync starts.
        client._transport.app.state.write_limits.manual.capacity = 3
        assert (
            await client.post("/api/dashboard/measurements", headers=HEADERS, json={})
        ).status_code == 422
        assert (await client.post("/sync/all", headers=HEADERS)).status_code == 202
        await asyncio.wait_for(entered.wait(), 30)
        assert (await client.get("/sync/status")).status_code == 200
        assert (await client.post("/sync/unknown", headers=HEADERS)).status_code == 404

        async def unread():
            pytest.fail("rate rejection must precede reading the body")
            yield b"unread"

        for path in ["/sync/all", "/sync/steps"]:
            response = await client.post(path, headers=HEADERS, content=unread())
            assert response.status_code == 429
            assert 1 <= int(response.headers["Retry-After"]) <= 60
        assert len(runtime.tasks) == 1


async def test_sync_body_limit_precedes_spawn(sessions):
    async with sync_application(sessions) as (client, runtime, entered, release):
        response = await client.post("/sync/all", headers=HEADERS, content=b"x" * 4097)
        assert response.status_code == 413
        assert not runtime.tasks


async def test_ownership_quotas_leave_capacity_for_nested_and_ordinary_work(tmp_path):
    engine = engine_module.create_shared_engine(
        Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}",
            sync_concurrency=2,
        )
    )
    try:
        async with AsyncExitStack() as stack:
            for namespace in (
                [NOTIFICATION_NAMESPACE] * 2 + [RUN_NAMESPACE] * 2 + [WATCHDOG_NAMESPACE]
            ):
                assert await stack.enter_async_context(ownership_connection(engine, namespace))
            assert engine.pool.checkedout() == 5
            for namespace in [NOTIFICATION_NAMESPACE, RUN_NAMESPACE, WATCHDOG_NAMESPACE]:
                async with ownership_connection(engine, namespace, wait=False) as connection:
                    assert connection is None

            # A blocked/cancelled owner consumes no physical slot.
            async def wait():
                async with ownership_connection(engine, RUN_NAMESPACE):
                    pytest.fail("quota must block this owner")

            blocked = asyncio.create_task(wait())
            await asyncio.sleep(0)
            assert not blocked.done() and engine.pool.checkedout() == 5
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
            for _ in range(2):
                connection = await stack.enter_async_context(engine.connect())
                assert await connection.scalar(text("SELECT 1")) == 1
            assert engine.pool.checkedout() == 7
        assert engine.pool.checkedout() == 0
        async with ownership_connection(engine, RUN_NAMESPACE, wait=False) as connection:
            assert connection is not None  # cancellation and cleanup returned the quota
    finally:
        await engine.dispose()
