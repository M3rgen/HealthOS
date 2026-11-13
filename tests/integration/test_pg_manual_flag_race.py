"""Concurrent writers of one measurement's flag on real PostgreSQL: one event per change."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.waits import blocked_on_a_lock

from app.models.manual_flags import FLAG_TABLE, ManualMeasurementFlagEvent
from app.services.archive.manual_flags import active_flags
from app.services.readers.dashboard_manual import create_measurement, set_measurement_flag

pytestmark = pytest.mark.integration


async def _measurement(sessions) -> uuid.UUID:
    async with sessions.begin() as session:
        created = await create_measurement(
            session, {"metric": "weight", "value": Decimal("70"), "measured_at": datetime.now(UTC)}
        )
    return uuid.UUID(created["id"])


async def _events(sessions, measurement: uuid.UUID) -> list[str]:
    """The measurement's actions in the order the readers take them."""
    async with sessions() as session:
        return list(
            await session.scalars(
                select(ManualMeasurementFlagEvent.action)
                .where(ManualMeasurementFlagEvent.measurement_id == measurement)
                .order_by(ManualMeasurementFlagEvent.created_at, ManualMeasurementFlagEvent.id)
            )
        )


@pytest.fixture
async def wide(pg_sessions):
    """A pool wide enough that every writer is truly in PostgreSQL at the same time."""
    engine = create_async_engine(pg_sessions.kw["bind"].url, pool_size=30, max_overflow=0)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_thirty_simultaneous_marks_append_one_event(wide):
    measurement = await _measurement(wide)

    async def mark() -> bool:
        async with wide.begin() as session:
            return (await set_measurement_flag(session, measurement, "test", True, None))["changed"]

    outcomes = await asyncio.gather(*(mark() for _ in range(30)))
    assert sum(outcomes) == 1
    assert await _events(wide, measurement) == ["mark"]


async def test_mixed_writers_leave_an_alternating_history_and_its_true_state(wide):
    measurement = await _measurement(wide)

    async def write(flagged: bool) -> bool:
        async with wide.begin() as session:
            return (await set_measurement_flag(session, measurement, "test", flagged, None))[
                "changed"
            ]

    wanted = [index % 2 == 0 for index in range(30)]
    outcomes = await asyncio.gather(*(write(flagged) for flagged in wanted))
    events = await _events(wide, measurement)
    assert sum(outcomes) == len(events) >= 1
    assert all(left != right for left, right in zip(events, events[1:], strict=False))
    assert events[0] == "mark"
    assert Counter(events)["mark"] - Counter(events)["unmark"] in (0, 1)
    async with wide() as session:
        flags = await active_flags(session, [measurement])
    assert flags.get(measurement, []) == (["test"] if events[-1] == "mark" else [])


async def test_two_measurements_do_not_queue_behind_each_other(pg_sessions):
    first, second = await _measurement(pg_sessions), await _measurement(pg_sessions)
    async with pg_sessions() as holder, pg_sessions() as other:
        await set_measurement_flag(holder, first, "test", True, None)
        async with asyncio.timeout(30):
            # Would wait for ``holder`` if the lock were not per measurement.
            await set_measurement_flag(other, second, "test", True, None)
        await other.commit()
        await holder.commit()


async def test_a_writer_that_began_first_but_waited_for_the_lock_is_ordered_last(pg_sessions):
    """The transaction that waited began earlier: its event must still be the state."""
    measurement = await _measurement(pg_sessions)
    async with pg_sessions() as late, pg_sessions() as early:
        # ``early`` has begun (now() is frozen) before ``late`` writes and commits the mark.
        assert await early.scalar(text("SELECT now()")) is not None
        await asyncio.sleep(0.01)
        await set_measurement_flag(late, measurement, "test", True, None)

        async def clear():
            result = await set_measurement_flag(early, measurement, "test", False, None)
            await early.commit()
            return result

        waiter = asyncio.create_task(clear())
        await blocked_on_a_lock(pg_sessions, waiter)
        await late.commit()
        result = await asyncio.wait_for(waiter, 60)
    assert result == {"id": str(measurement), "flags": [], "changed": True}
    assert await _events(pg_sessions, measurement) == ["mark", "unmark"]
    async with pg_sessions() as session:
        assert not await active_flags(session, [measurement])


async def test_older_events_written_without_the_lock_are_left_as_they_are(pg_sessions):
    """Duplicates from before the lock stay; the next change follows them by state."""
    measurement = await _measurement(pg_sessions)
    async with pg_sessions.begin() as session:
        for _ in range(3):
            await session.execute(
                text(
                    f"INSERT INTO {FLAG_TABLE} (measurement_id, flag, action) "
                    "VALUES (:measurement, 'test', 'mark')"
                ),
                {"measurement": measurement},
            )
    async with pg_sessions.begin() as session:
        unchanged = await set_measurement_flag(session, measurement, "test", True, None)
    assert unchanged["changed"] is False and unchanged["flags"] == ["test"]
    async with pg_sessions.begin() as session:
        cleared = await set_measurement_flag(session, measurement, "test", False, None)
    assert cleared == {"id": str(measurement), "flags": [], "changed": True}
    assert await _events(pg_sessions, measurement) == ["mark", "mark", "mark", "unmark"]
