"""Behaviour races that die when a lock is removed; no outcome depends on elapsed time.

``_lock_rows`` must hold its rows until commit: ``FOR UPDATE NOWAIT`` from another connection
fails at once when, and only when, the page holds them. The commit-time drain must lock days
in key order, then bump them: a writer that blocks on the first day holds none of the later
ones, which a NOWAIT probe sees without any timing. PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from identity_records import HEART, heart, raw_rows, store, versions_of
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from support.waits import blocked_on_a_lock

from app.core.providers import GOOGLE
from app.models.derived import DerivedDay
from app.models.health import Steps
from app.services.archive import storage
from app.services.interpretation.derived import register_range

pytestmark = pytest.mark.integration

FIRST_DAY = date(2026, 9, 1)
TIMEZONE = "UTC"
DEADLINE = 30.0
LOCK_NOT_AVAILABLE = "55P03"
TAKE_ROW = text("SELECT 1 FROM raw_health_data WHERE id = :id FOR UPDATE NOWAIT")
TAKE_DAY = text("SELECT 1 FROM derived_days WHERE day = :day FOR UPDATE NOWAIT")
# The drain's plan visits physical order: index scans would come back sorted (ascending days).
PHYSICAL_ORDER = (
    text("SET LOCAL enable_indexscan = off"),
    text("SET LOCAL enable_bitmapscan = off"),
)


def _paused_lock_rows(monkeypatch):
    """Pause the first page right after ``_lock_rows`` took its row locks."""
    locked, proceed = asyncio.Event(), asyncio.Event()
    original = storage._lock_rows
    calls: list[int] = []

    async def paused(session, spec, google_ids, resolution):
        found = await original(session, spec, google_ids, resolution)
        calls.append(1)
        if len(calls) == 1:
            locked.set()
            await asyncio.wait_for(proceed.wait(), DEADLINE)
        return found

    monkeypatch.setattr(storage, "_lock_rows", paused)
    return locked, proceed, calls


async def test_rows_a_page_writes_onto_stay_locked_until_it_commits(pg_sessions, monkeypatch):
    await store(pg_sessions, HEART, heart(60))
    [row] = await raw_rows(pg_sessions, HEART)
    locked, proceed, _ = _paused_lock_rows(monkeypatch)
    writer = asyncio.create_task(store(pg_sessions, HEART, heart(61)))
    await asyncio.wait_for(locked.wait(), DEADLINE)

    with pytest.raises(DBAPIError) as refused:
        async with pg_sessions.begin() as session:
            await session.execute(TAKE_ROW, {"id": row.id})
    assert refused.value.orig.sqlstate == LOCK_NOT_AVAILABLE

    proceed.set()
    await asyncio.wait_for(writer, DEADLINE)
    [row] = await raw_rows(pg_sessions, HEART)
    assert row.raw_json == heart(61)


async def test_a_second_writer_waits_and_keeps_the_first_payload_as_a_version(
    pg_sessions, monkeypatch
):
    """Without the lock the second writer goes straight past and the first's version is lost."""
    await store(pg_sessions, HEART, heart(60))
    locked, proceed, calls = _paused_lock_rows(monkeypatch)
    first = asyncio.create_task(store(pg_sessions, HEART, heart(61)))
    await asyncio.wait_for(locked.wait(), DEADLINE)

    second = asyncio.create_task(store(pg_sessions, HEART, heart(62)))
    await blocked_on_a_lock(pg_sessions, second)
    assert not second.done()
    proceed.set()
    await asyncio.wait_for(first, DEADLINE)
    await asyncio.wait_for(second, DEADLINE)

    [row] = await raw_rows(pg_sessions, HEART)
    assert row.raw_json == heart(62)
    assert [v["heartRate"]["beatsPerMinute"] for v in await versions_of(pg_sessions, row.id)] == [
        60,
        61,
    ]
    assert len(calls) >= 2


def _steps_on(day: date) -> Steps:
    start = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
    return Steps(
        google_data_point_id=f"synthetic-steps-{uuid.uuid4().hex}",
        provider=GOOGLE,
        start_time=start,
        end_time=start + timedelta(minutes=30),
        count=100,
    )


async def test_the_commit_drain_locks_days_in_key_order(pg_sessions):
    """A writer blocked on the first day of a set holds none of the later ones.

    Days are registered and dirtied latest-first, so an unordered bump visits them
    latest-first: blocked on the earliest day, it would already hold every other one.
    """
    days = [FIRST_DAY + timedelta(days=offset) for offset in range(4)]
    async with pg_sessions.begin() as session:
        for day in reversed(days):  # physical order is reversed key order
            await register_range(session, day, day, TIMEZONE)

    async with pg_sessions() as blocker, blocker.begin():
        await blocker.execute(TAKE_DAY, {"day": days[0]})  # the earliest key: every drain's first

        async def write() -> None:
            async with pg_sessions.begin() as session:
                for statement in PHYSICAL_ORDER:
                    await session.execute(statement)
                for day in reversed(days):  # pending rows too are latest-first
                    session.add(_steps_on(day))
                    await session.flush()

        writer = asyncio.create_task(write())
        await blocked_on_a_lock(pg_sessions, writer)  # at the commit drain, on days[0]

        # A drain that took days in key order holds none of the later days yet.
        for day in days[1:]:
            async with pg_sessions.begin() as session:
                await session.execute(TAKE_DAY, {"day": day})

    await asyncio.wait_for(writer, DEADLINE)
    async with pg_sessions() as session:
        revisions = dict((await session.execute(select(DerivedDay.day, DerivedDay.revision))).all())
    assert revisions == dict.fromkeys(days, 2)  # one commit: 1 + 1
