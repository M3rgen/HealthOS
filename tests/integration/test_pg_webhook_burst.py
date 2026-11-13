"""A burst of deliveries on the default limit: everything finishes, nothing runs twice."""

import asyncio
import collections
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.waits import until
from support.webhooks import notification

from app.google_health.data_types import REGISTRY
from app.models.state import OAuthToken, WebhookBatchItem, WebhookEvent, WebhookNotification
from app.sync.webhook_processing import INTERRUPTED_ERROR, WebhookBatchProcessor

pytestmark = pytest.mark.integration

LIMIT = 2  # the default sync_concurrency
BASE = datetime(2026, 11, 1, tzinfo=UTC)


class Incremental:
    """Counts windows; a run may be held open to keep an owner slot occupied."""

    specs = dict(REGISTRY)

    def __init__(self):
        self.runs = collections.Counter()
        self.active = 0
        self.peak = 0
        self.hold: asyncio.Event | None = None

    async def run(self, data_type, start=None, end=None, *, check_owner=None):
        self.runs[start] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.hold is not None:
                await self.hold.wait()
            await asyncio.sleep(0.01)  # a request to Google
        finally:
            self.active -= 1


def limited(sessions):
    sessions.kw["bind"].update_execution_options(ownership_concurrency=LIMIT)
    return sessions


async def deliver(runner, count, *, first=0):
    """Ingest ``count`` distinct notifications (one hour each); their batch hashes."""
    async with runner.sessions.begin() as session:
        if await session.get(OAuthToken, "me") is None:
            session.add(
                OAuthToken(
                    subject="me", encrypted_refresh_token="synthetic", health_user_id="user-1"
                )
            )
    hashes = []
    for hour in range(first, first + count):
        payload = notification("steps")
        window = payload["data"]["intervals"][0]["physicalTimeInterval"]
        window.update(
            startTime=(BASE + timedelta(hours=hour)).isoformat(),
            endTime=(BASE + timedelta(hours=hour + 1)).isoformat(),
        )
        result = await runner.ingest_verified(json.dumps(payload).encode(), payload)
        hashes.append(result.batch_hash)
    return hashes


async def statuses(sessions):
    async with sessions() as session:
        rows = await session.scalars(select(WebhookNotification.status))
        return collections.Counter(rows.all())


async def unfinished_batches(sessions):
    async with sessions() as session:
        return len(
            (
                await session.scalars(
                    select(WebhookEvent.payload_hash).where(WebhookEvent.processed_at.is_(None))
                )
            ).all()
        )


async def age_all(sessions):
    """Make every element old enough for the sweep (a delivery of minutes ago)."""
    stale = datetime.now(UTC) - timedelta(hours=1)
    async with sessions.begin() as session:
        await session.execute(update(WebhookNotification).values(updated_at=stale))


async def test_six_simultaneous_deliveries_all_finish_exactly_once(pg_sessions):
    sessions = limited(pg_sessions)
    incremental = Incremental()
    runner = WebhookBatchProcessor(sessions, incremental, set(REGISTRY))
    hashes = await deliver(runner, 6)

    results = await asyncio.gather(*(runner.process_batch(h) for h in hashes))

    assert [r.completed_elements for r in results] == [1] * 6
    assert await statuses(sessions) == {"completed": 6}
    assert await unfinished_batches(sessions) == 0
    assert sorted(incremental.runs.values()) == [1] * 6, "every element ran exactly once"
    assert incremental.peak <= LIMIT, "the limit still bounds what runs at once"


async def test_queued_deliveries_hold_no_connection_while_waiting(pg_sessions):
    sessions = limited(pg_sessions)
    engine = sessions.kw["bind"]
    incremental = Incremental()
    incremental.hold = asyncio.Event()
    runner = WebhookBatchProcessor(sessions, incremental, set(REGISTRY))
    hashes = await deliver(runner, 6)

    tasks = [asyncio.create_task(runner.process_batch(h)) for h in hashes]
    await until(lambda: incremental.active == LIMIT, "two owners to start")
    await until(lambda: engine.pool.checkedout() <= LIMIT, "only the owners to hold connections")
    assert await statuses(sessions) == {"running": 2, "pending": 4}, "four wait, none is lost"

    incremental.hold.set()
    await asyncio.gather(*tasks)
    assert await statuses(sessions) == {"completed": 6}


async def test_a_second_process_never_runs_an_element_twice(pg_sessions):
    limited(pg_sessions)
    engine = create_async_engine(pg_sessions.kw["bind"].url)
    engine.update_execution_options(ownership_concurrency=LIMIT)
    other_sessions = async_sessionmaker(engine, expire_on_commit=False)
    incremental = Incremental()
    try:
        runner = WebhookBatchProcessor(pg_sessions, incremental, set(REGISTRY))
        other = WebhookBatchProcessor(other_sessions, incremental, set(REGISTRY))
        hashes = await deliver(runner, 6)

        await asyncio.gather(
            *(runner.process_batch(h) for h in hashes), *(other.process_batch(h) for h in hashes)
        )

        assert await statuses(pg_sessions) == {"completed": 6}
        assert sorted(incremental.runs.values()) == [1] * 6
    finally:
        await engine.dispose()


async def test_the_sweep_finishes_what_a_shutdown_left_pending(pg_sessions):
    sessions = limited(pg_sessions)
    incremental = Incremental()
    runner = WebhookBatchProcessor(sessions, incremental, set(REGISTRY))
    hashes = await deliver(runner, 3)  # ingested, but the spawned tasks never ran
    await age_all(sessions)

    swept = await runner.process_unfinished()

    assert swept.completed_elements == 3
    assert await statuses(sessions) == {"completed": 3}
    assert await unfinished_batches(sessions) == 0
    assert sorted(incremental.runs.values()) == [1] * 3
    # Nothing is left for a second sweep; a delivered batch is not repeated by it either.
    assert (await runner.process_unfinished()).completed_elements == 0
    assert sum(incremental.runs.values()) == 3
    assert len(hashes) == 3


async def test_a_queued_delivery_cancelled_at_shutdown_is_picked_up_by_the_sweep(pg_sessions):
    sessions = limited(pg_sessions)
    incremental = Incremental()
    incremental.hold = asyncio.Event()
    runner = WebhookBatchProcessor(sessions, incremental, set(REGISTRY))
    hashes = await deliver(runner, 4)

    tasks = [asyncio.create_task(runner.process_batch(h)) for h in hashes]
    await until(lambda: incremental.active == LIMIT, "two owners to start")
    # Which two own a slot depends on the pool's order: ask the database who is still waiting.
    assert await statuses(sessions) == {"running": 2, "pending": 2}
    async with sessions() as session:
        waiting = set(
            await session.scalars(
                select(WebhookBatchItem.batch_hash)
                .join(
                    WebhookNotification,
                    WebhookNotification.element_hash == WebhookBatchItem.element_hash,
                )
                .where(WebhookNotification.status == "pending")
            )
        )
    queued = [task for task, batch in zip(tasks, hashes, strict=True) if batch in waiting]
    for task in queued:
        task.cancel()
    await asyncio.gather(*queued, return_exceptions=True)
    incremental.hold.set()
    await asyncio.gather(*(t for t in tasks if t not in queued))
    assert await statuses(sessions) == {"completed": 2, "pending": 2}

    await age_all(sessions)
    assert (await runner.process_unfinished()).completed_elements == 2
    assert await statuses(sessions) == {"completed": 4}
    assert sorted(incremental.runs.values()) == [1] * 4


async def test_the_sweep_and_a_delivery_of_one_element_run_it_once(pg_sessions):
    sessions = limited(pg_sessions)
    incremental = Incremental()
    runner = WebhookBatchProcessor(sessions, incremental, set(REGISTRY))
    [batch] = await deliver(runner, 1)
    await age_all(sessions)

    await asyncio.gather(runner.process_batch(batch), runner.process_unfinished())

    assert await statuses(sessions) == {"completed": 1}
    assert sum(incremental.runs.values()) == 1


async def test_the_sweep_retries_only_interrupted_failures(pg_sessions):
    sessions = limited(pg_sessions)
    incremental = Incremental()
    runner = WebhookBatchProcessor(sessions, incremental, set(REGISTRY))
    await deliver(runner, 2)
    async with sessions.begin() as session:
        rows = list(await session.scalars(select(WebhookNotification)))
        rows[0].status, rows[0].last_error = "failed", INTERRUPTED_ERROR
        rows[1].status, rows[1].last_error = "failed", "HTTP 500 from the source"
    await age_all(sessions)

    assert (await runner.process_unfinished()).completed_elements == 1
    assert await statuses(sessions) == {"completed": 1, "failed": 1}
    async with sessions() as session:
        still = await session.get(WebhookNotification, rows[1].element_hash)
    assert still.last_error == "HTTP 500 from the source"
