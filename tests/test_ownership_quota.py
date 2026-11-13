"""The queueing shared by both backends: a busy key holds no owner slot, the limit counts
owners, and an abandoned wait leaks nothing. Only the lock primitive differs by backend."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.waits import until

from app.sync import ownership
from app.sync.ownership import RUN_NAMESPACE, run_ownership

LIMIT = "ownership_concurrency"


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quota.db'}")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _own(sessions, key, entered, release):
    async with run_ownership(sessions, key) as owned:
        assert owned is not None
        entered.set()
        await release.wait()


def _start(sessions, key):
    entered, release = asyncio.Event(), asyncio.Event()
    return asyncio.create_task(_own(sessions, key, entered, release)), entered, release


async def test_a_waiter_on_a_busy_type_takes_no_owner_slot(sessions):
    sessions.kw["bind"].update_execution_options(**{LIMIT: 2})
    owner, owner_in, release_owner = _start(sessions, "steps")
    await asyncio.wait_for(owner_in.wait(), 30)
    contended = asyncio.Event()
    original = ownership._contended

    async def noticed():
        contended.set()

    ownership._contended = noticed
    try:
        waiter, waiter_in, release_waiter = _start(sessions, "steps")
        await asyncio.wait_for(contended.wait(), 30)
        other, other_in, release_other = _start(sessions, "sleep")
        await asyncio.wait_for(other_in.wait(), 30)  # the limit of 2 still lets it run
        release_other.set()
        await asyncio.wait_for(other, 30)
        assert not waiter_in.is_set()
    finally:
        ownership._contended = original
    release_owner.set()
    await asyncio.wait_for(waiter_in.wait(), 30)
    release_waiter.set()
    await asyncio.wait_for(asyncio.gather(owner, waiter), 30)


async def test_the_limit_counts_owners_and_a_free_type_waits_for_a_slot(sessions):
    sessions.kw["bind"].update_execution_options(**{LIMIT: 1})
    first, first_in, release_first = _start(sessions, "steps")
    await asyncio.wait_for(first_in.wait(), 30)
    second, second_in, release_second = _start(sessions, "sleep")
    quota = ownership._quota(sessions.kw["bind"], RUN_NAMESPACE)
    await until(lambda: len(quota.owners._waiters or ()) == 1, "the run queued for a slot")
    assert not second_in.is_set()
    release_first.set()
    await asyncio.wait_for(second_in.wait(), 30)
    release_second.set()
    await asyncio.wait_for(asyncio.gather(first, second), 30)


async def test_a_cancelled_waiter_leaves_neither_a_gate_entry_nor_a_slot(sessions):
    sessions.kw["bind"].update_execution_options(**{LIMIT: 1})
    owner, owner_in, release_owner = _start(sessions, "steps")
    await asyncio.wait_for(owner_in.wait(), 30)
    waiter, _, _ = _start(sessions, "steps")
    await until(
        lambda: any(entry.users > 1 for entry in ownership._local_locks.values()),
        "the waiter queued at the gate",
    )
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release_owner.set()
    await asyncio.wait_for(owner, 30)
    assert ownership._local_locks == {}
    async with run_ownership(sessions, "sleep", wait=False) as owned:
        assert owned is not None  # the slot was returned


async def test_a_busy_key_of_this_process_uses_no_connection():
    engine = Mock(spec=ownership.AsyncEngine)
    engine.dialect = SimpleNamespace(name="postgresql")
    engine.get_execution_options.return_value = {LIMIT: 2}
    connection = AsyncMock(spec=ownership.AsyncConnection)
    connection.closed = connection.invalidated = False
    connection.scalar.return_value = True
    connection.begin = Mock(return_value=AsyncMock())
    engine.connect = AsyncMock(return_value=connection)
    sessions = SimpleNamespace(kw={"bind": engine})

    async with run_ownership(sessions, "steps") as owned:
        assert owned is not None
        async with run_ownership(sessions, "steps", wait=False) as second:
            assert second is None
    engine.connect.assert_awaited_once()
