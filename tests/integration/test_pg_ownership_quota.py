"""Waiting for one data type never holds up another; the limit counts runs that hold a lock.

Separate engines stand for separate processes, as in ``test_pg_sync_ownership``.
Each test waits on PostgreSQL's own state (``pg_locks``, lock waiters), never on elapsed time.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from support.waits import until

from app.sync import ownership
from app.sync.ownership import RUN_NAMESPACE, run_ownership

pytestmark = pytest.mark.integration

ACQUIRE = 30.0
GRANTED = text(
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
    "WHERE a.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
    "AND l.classid = :namespace"
)
WAITING = text(
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND wait_event_type = 'Lock' AND query LIKE 'SELECT pg_advisory_lock(%'"
)


@pytest_asyncio.fixture
async def other_process(pg_sessions) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(pg_sessions.kw["bind"].url)
    engine.update_execution_options(ownership_concurrency=2)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def limit(sessions, concurrency: int) -> AsyncEngine:
    engine = sessions.kw["bind"]
    engine.update_execution_options(ownership_concurrency=concurrency)
    return engine


async def granted(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(GRANTED, {"namespace": RUN_NAMESPACE})


async def waiting(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(WAITING)


def queued(engine: AsyncEngine, namespace: int = RUN_NAMESPACE) -> int:
    """Coroutines waiting for an owner slot (a white-box view of the limit)."""
    return sum(not w.cancelled() for w in ownership._quota(engine, namespace).owners._waiters or ())


@asynccontextmanager
async def held(sessions, data_type: str):
    async with run_ownership(sessions, data_type) as owned:
        assert owned is not None
        yield owned


async def own_until(sessions, data_type: str, release: asyncio.Event, entered: asyncio.Event):
    async with held(sessions, data_type):
        entered.set()
        await release.wait()


async def test_a_waiter_on_a_busy_type_leaves_the_limit_to_another_type(pg_sessions):
    """Same process, limit 2: one owner plus one waiter of ``steps`` used to fill the limit."""
    limit(pg_sessions, 2)
    release = asyncio.Event()
    owner_in = asyncio.Event()
    owner = asyncio.create_task(own_until(pg_sessions, "steps", release, owner_in))
    await asyncio.wait_for(owner_in.wait(), ACQUIRE)
    contended = asyncio.Event()
    original = ownership._contended

    async def noticed():
        contended.set()

    ownership._contended = noticed
    waiter_in = asyncio.Event()
    try:
        waiter = asyncio.create_task(own_until(pg_sessions, "steps", release, waiter_in))
        await asyncio.wait_for(contended.wait(), ACQUIRE)
        # While the waiter queues, another type runs at once.
        async with asyncio.timeout(ACQUIRE):
            async with held(pg_sessions, "sleep") as sessions:
                async with sessions.begin() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
        assert not owner.done() and not waiter.done() and not waiter_in.is_set()
    finally:
        ownership._contended = original
        release.set()
        await asyncio.wait_for(asyncio.gather(owner, waiter), ACQUIRE)
    assert waiter_in.is_set()


async def test_types_owned_by_another_process_do_not_hold_up_a_free_type(
    pg_sessions, other_process
):
    """Limit 2, both types held elsewhere: the two parked waiters occupy no owner slot."""
    limit(pg_sessions, 2)
    release = asyncio.Event()
    owners = []
    for data_type in ("steps", "sleep"):
        entered = asyncio.Event()
        owners.append(asyncio.create_task(own_until(other_process, data_type, release, entered)))
        await asyncio.wait_for(entered.wait(), ACQUIRE)
    waiters = []
    granted_events = {"steps": asyncio.Event(), "sleep": asyncio.Event()}
    never = asyncio.Event()
    for data_type, event in granted_events.items():
        waiters.append(asyncio.create_task(own_until(pg_sessions, data_type, never, event)))
    await until(lambda: _exactly(pg_sessions, 2), "both waiters parked in the server queue")
    # Both waiters are parked; a free type takes an owner slot at once.
    async with asyncio.timeout(ACQUIRE):
        async with held(pg_sessions, "heart-rate"):
            assert await granted(pg_sessions) == 3  # two elsewhere + heart-rate
    assert not any(event.is_set() for event in granted_events.values())
    release.set()
    await asyncio.wait_for(asyncio.gather(*owners), ACQUIRE)
    for event in granted_events.values():
        await asyncio.wait_for(event.wait(), ACQUIRE)
    # Granted waiters hold the limit: both run.
    assert await granted(pg_sessions) == 2
    never.set()
    await asyncio.wait_for(asyncio.gather(*waiters), ACQUIRE)
    assert await granted(pg_sessions) == 0


async def test_the_limit_counts_granted_runs_of_free_types(pg_sessions):
    """Limit 2 with three free types: the third starts only when one owner ends."""
    engine = limit(pg_sessions, 2)
    releases = {name: asyncio.Event() for name in ("steps", "sleep", "heart-rate")}
    entered = {name: asyncio.Event() for name in releases}
    running = 0
    high = 0

    async def run(data_type):
        nonlocal running, high
        async with held(pg_sessions, data_type):
            running += 1
            high = max(high, running)
            entered[data_type].set()
            await releases[data_type].wait()
            running -= 1

    tasks = {name: asyncio.create_task(run(name)) for name in releases}
    await until(lambda: sum(e.is_set() for e in entered.values()) == 2, "two owners running")
    await until(lambda: queued(engine) == 1, "the third run waiting for an owner slot")
    third = next(name for name, event in entered.items() if not event.is_set())
    assert high == 2 and await granted(pg_sessions) == 2
    first = next(name for name, event in entered.items() if event.is_set())
    releases[first].set()
    await asyncio.wait_for(tasks[first], ACQUIRE)
    await asyncio.wait_for(entered[third].wait(), ACQUIRE)
    assert high == 2
    for event in releases.values():
        event.set()
    await asyncio.wait_for(asyncio.gather(*tasks.values()), ACQUIRE)


async def test_parked_waiters_are_bounded_and_the_rest_retry_behind_them(
    pg_sessions, other_process
):
    """Limit 1: one parked waiter; the next key retries instead of queueing behind it."""
    limit(pg_sessions, 1)
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    for data_type, release in (("steps", release_a), ("sleep", release_b)):
        entered = asyncio.Event()
        asyncio.create_task(own_until(other_process, data_type, release, entered))
        await asyncio.wait_for(entered.wait(), ACQUIRE)
    a_in = asyncio.Event()
    b_in = asyncio.Event()
    never = asyncio.Event()
    a = asyncio.create_task(own_until(pg_sessions, "steps", never, a_in))
    await until(lambda: _exactly(pg_sessions, 1), "the steps waiter parked")
    b = asyncio.create_task(own_until(pg_sessions, "sleep", never, b_in))
    # sleep's holder leaves first, so its retry must take it even though steps stays parked.
    release_b.set()
    await asyncio.wait_for(b_in.wait(), ACQUIRE)
    # Its queue is cut into slices (app/sync/ownership.py, WAIT_SLICE): between two slices the
    # waiter is not in the server's queue, so one count can land in the gap on a loaded server.
    await until(lambda: _exactly(pg_sessions, 1), "the steps waiter parked again")
    assert not a_in.is_set()
    never.set()
    await asyncio.wait_for(b, ACQUIRE)
    release_a.set()
    await asyncio.wait_for(a_in.wait(), ACQUIRE)
    await asyncio.wait_for(a, ACQUIRE)


async def _exactly(sessions, count: int) -> bool:
    return await waiting(sessions) == count


async def test_a_waiter_cancelled_after_its_grant_keeps_no_slot_and_no_connection(
    pg_sessions, other_process
):
    """Limit 1, owner running: a waiter granted the lock waits for the slot; cancelled there,
    the lock is released with its connection and the limit is intact."""
    engine = limit(pg_sessions, 1)
    release_busy = asyncio.Event()
    busy_in = asyncio.Event()
    busy = asyncio.create_task(own_until(other_process, "sleep", release_busy, busy_in))
    await asyncio.wait_for(busy_in.wait(), ACQUIRE)
    waiter_in = asyncio.Event()
    hold = asyncio.Event()
    waiter = asyncio.create_task(own_until(pg_sessions, "sleep", hold, waiter_in))
    await until(lambda: _exactly(pg_sessions, 1), "the sleep waiter parked")
    running_in = asyncio.Event()
    running = asyncio.create_task(own_until(pg_sessions, "steps", hold, running_in))
    await asyncio.wait_for(running_in.wait(), ACQUIRE)
    release_busy.set()
    await asyncio.wait_for(busy, ACQUIRE)
    await until(lambda: queued(engine) == 1, "the granted waiter queues for the slot")
    assert not waiter_in.is_set()
    assert await granted(pg_sessions) == 2  # the running owner and the granted waiter
    # Its connection still counts as parked, so the pool arithmetic holds until it runs.
    assert ownership._quota(engine, RUN_NAMESPACE).parked.locked()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await until(lambda: _granted_exactly(pg_sessions, 1), "the cancelled grant released")
    hold.set()
    await asyncio.wait_for(running, ACQUIRE)
    async with held(pg_sessions, "sleep"):
        assert await granted(pg_sessions) == 1


async def _granted_exactly(sessions, count: int) -> bool:
    return await granted(sessions) == count
