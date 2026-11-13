"""Bounded waits for concurrency tests: poll for the state a test needs instead of sleeping.

A fixed ``sleep`` either wastes time or, on a loaded machine, is too short and the test passes
without ever reaching the race it is about. These helpers return as soon as the condition holds
and fail with a description once the deadline passes (a deadlock guard, not a speed claim).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text

# A deadlock guard, not a speed claim: a loaded machine may need a good while.
DEADLINE = 60.0
# Poll interval: short enough to add no noticeable latency, long enough not to spin.
INTERVAL = 0.01

# Backends of this database (not the probe itself) that wait for a heavyweight lock: a row
# (transactionid/tuple), a table or an advisory lock.
_LOCK_WAITERS = text(
    "SELECT pid, query FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
    "AND wait_event_type = 'Lock'"
)
# Backends of this database whose current or last statement contains the given text.
_RUNNING = text(
    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() AND strpos(query, :fragment) > 0"
)


async def until(
    condition: Callable[[], object | Awaitable[object]],
    description: str,
    *,
    deadline: float = DEADLINE,
) -> object:
    """Poll ``condition`` (sync or async) until it is truthy; its value is returned."""
    give_up = time.monotonic() + deadline
    while True:
        value = condition()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        assert time.monotonic() < give_up, f"timed out after {deadline} s waiting for {description}"
        await asyncio.sleep(INTERVAL)  # poll interval, see INTERVAL


async def lock_waiters(sessions) -> list[tuple[int, str]]:
    """``(pid, query)`` of every other backend of the test database waiting for a lock."""
    async with sessions() as session:
        return [(pid, query) for pid, query in (await session.execute(_LOCK_WAITERS)).all()]


async def blocked_on_a_lock(sessions, *tasks: asyncio.Task) -> list[tuple[int, str]]:
    """Wait until another backend waits for a lock (PostgreSQL ``pg_stat_activity``).

    Fails early when one of ``tasks`` finished meanwhile: the statement it should block on
    did not block (its exception, if any, is raised)."""

    async def blocked():
        for task in tasks:
            if task.done():
                task.result()
                raise AssertionError("the task finished instead of waiting for the lock")
        return await lock_waiters(sessions)

    return await until(blocked, "a backend waiting for a lock")  # type: ignore[return-value]


async def blocked_or_done(sessions, task: asyncio.Task) -> None:
    """Wait until ``task`` either waits for a lock or has finished (both are allowed)."""

    async def settled():
        return task.done() or await lock_waiters(sessions)

    await until(settled, "the task to wait for a lock or finish")


async def running(sessions, fragment: str) -> list[int]:
    """Wait until another backend has sent a statement containing ``fragment``."""

    async def seen():
        async with sessions() as session:
            return (await session.scalars(_RUNNING, {"fragment": fragment})).all()

    return await until(seen, f"a backend to run {fragment!r}")  # type: ignore[return-value]
