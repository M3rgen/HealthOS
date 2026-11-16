"""The one-pass initial fill writes exactly the packets the per-hour builder writes."""

import asyncio
import random
from datetime import timedelta

import pytest
from derived_records import point
from sqlalchemy import func, select, text, update
from test_pg_heart_projection_review import BASE, mutate, record, store

from app.models.health import HeartRate
from app.models.heart_projection import HeartHour, HeartProjectionState
from app.models.state import RawHealthData
from app.services.readers import current_heart, heart_fill, heart_maintenance, heart_projection
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.heart_maintenance import backfill_heart_hours, refresh_heart_hours

pytestmark = pytest.mark.integration


async def archive(sessions, seed, names=60, mutations=40):
    """Random originals, corrections, ties, quarantine, deletion, links and devices."""
    rng = random.Random(seed)
    tie = {"sampleTime": {"physicalTime": BASE.isoformat()}}  # equal raw times: id order
    ties = [point("heart-rate", f"tie{i}", {**tie, "bpm": 60 + i}, {}) for i in range(4)]
    await store(sessions, [record(rng, f"r{i}") for i in range(names)] + ties)
    for _ in range(mutations):
        if rng.random() < 0.25:
            await store(sessions, [record(rng, f"r{rng.randrange(names)}") for _ in range(5)])
        await mutate(sessions, rng, rollback=rng.random() < 0.1)
    async with sessions.begin() as session:  # holders hidden by quarantine, not deletion
        await session.execute(
            text(
                "UPDATE raw_health_data SET is_quarantined = true WHERE id IN (SELECT new_raw_id "
                "FROM raw_health_data_supersessions WHERE old_fingerprint = new_fingerprint "
                "ORDER BY old_raw_id LIMIT 3)"
            )
        )


async def populated(sessions):
    async with sessions() as session:
        times = await session.scalars(
            select(RawHealthData.source_start_time).where(RawHealthData.data_type == "heart-rate")
        )
        return {heart_projection.hour(moment) for moment in times if moment is not None}


async def per_hour(sessions):
    """The per-hour builder's packet for every populated hour (the reference)."""
    expected = {}
    for bucket in sorted(await populated(sessions)):
        async with sessions() as session:
            expected[bucket] = await heart_maintenance._pack(session, bucket)
    return expected


async def assert_equal_to_per_hour(sessions, exact=True):
    expected = await per_hour(sessions)
    async with sessions() as session:
        state = await session.get(HeartProjectionState, 1)
        rows = {row.bucket: row for row in await session.scalars(select(HeartHour))}
    assert state.ready
    # Writers register every hour they touch; an emptied hour keeps no packet until repair
    # (it stays dirty and reads the empty raw hour), and repair gives it an empty one.
    built = {bucket for bucket, row in rows.items() if row.samples is not None}
    assert built == set(expected) if exact else built >= set(expected)
    assert all(rows[bucket].built_revision != rows[bucket].revision for bucket in set(rows) - built)
    expected.update({bucket: [] for bucket in built - set(expected)})
    for bucket, packed in expected.items():
        row = rows[bucket]
        assert row.samples == packed, bucket
        assert row.digest == heart_projection.digest(packed)
        assert row.built_revision == row.revision and row.method == heart_projection.METHOD


@pytest.mark.parametrize("seed", range(8))
async def test_one_pass_fill_equals_the_per_hour_builder(pg_sessions, seed, monkeypatch):
    await archive(pg_sessions, seed)
    # Small commits: every packet crosses several cursor commits.
    monkeypatch.setattr(heart_maintenance, "FLUSH_HOURS", 1 + seed % 3)
    monkeypatch.setattr(heart_maintenance, "FLUSH_SAMPLES", 2 + seed)
    progress = heart_fill.FillProgress()
    assert await heart_maintenance.fill_space_needed(pg_sessions) > 0
    assert not await heart_maintenance.fill_is_ready(pg_sessions)
    written = await backfill_heart_hours(pg_sessions, progress=progress)
    assert await heart_maintenance.fill_is_ready(pg_sessions)
    assert written == progress.packets == progress.hours == len(await populated(pg_sessions))
    await assert_equal_to_per_hour(pg_sessions)
    assert await backfill_heart_hours(pg_sessions) == 0


async def old_fill(sessions, limit):
    """The 0.2.4 builder: one fenced transaction per populated hour, cursor to the next."""
    hours = sorted(await populated(sessions))
    for _ in range(limit):
        async with sessions.begin() as session:
            assert await heart_maintenance.fence(session)
            state = await heart_maintenance._initialize(session)
            await heart_maintenance._build(session, state.next_hour)
            later = [hour for hour in hours if hour > state.next_hour]
            state.next_hour = later[0] if later else state.stop
            state.ready = state.next_hour >= state.stop


async def test_fill_resumes_over_packets_of_the_per_hour_builder(pg_sessions):
    await archive(pg_sessions, 99)
    await old_fill(pg_sessions, 6)  # production stopped after six hours
    async with pg_sessions() as session:
        assert not (await session.get(HeartProjectionState, 1)).ready
        assert await session.scalar(select(func.count(HeartHour.samples))) == 6
    assert await backfill_heart_hours(pg_sessions) == len(await populated(pg_sessions)) - 6
    await assert_equal_to_per_hour(pg_sessions)


async def test_writes_after_the_snapshot_stay_dirty_and_are_repaired(pg_sessions, monkeypatch):
    await archive(pg_sessions, 7, mutations=0)
    original = heart_fill.revisions
    late = BASE + timedelta(days=30)

    async def concurrent(session, start, stop):
        found = await original(session, start, stop)  # the reader's snapshot exists now
        await store(pg_sessions, [record(random.Random(1), "late")])  # maybe a new hour
        async with pg_sessions.begin() as writer:
            await writer.execute(update(RawHealthData).values(is_quarantined=True))
            await writer.execute(
                update(RawHealthData)
                .where(RawHealthData.google_data_point_id.like("%/r0"))
                .values(source_start_time=late)
            )
        return found

    monkeypatch.setattr(heart_fill, "revisions", concurrent)
    await backfill_heart_hours(pg_sessions)
    monkeypatch.setattr(heart_fill, "revisions", original)
    async with pg_sessions() as session:
        assert (await session.get(HeartProjectionState, 1)).ready
        rows = list(await session.scalars(select(HeartHour)))
        # Every hour was touched after the snapshot: none may be served.
        assert rows and all(row.built_revision != row.revision for row in rows)
        assert {heart_projection.hour(late)} <= {row.bucket for row in rows}
        predicate = DashboardSources().normalized(HeartRate, "heart-rate")
        projected = await current_heart.chart_heart_rate_samples(
            session, BASE - timedelta(days=1), late + timedelta(days=1), predicate
        )
        assert projected == []
    while await refresh_heart_hours(pg_sessions, limit=168):
        pass
    await assert_equal_to_per_hour(pg_sessions, exact=False)


async def test_halt_and_cancellation_resume_at_the_last_commit(pg_sessions, monkeypatch):
    await archive(pg_sessions, 3, mutations=10)
    monkeypatch.setattr(heart_maintenance, "FLUSH_HOURS", 1)
    halt, original = asyncio.Event(), heart_fill.write

    async def stop_after_first(*args):
        await original(*args)
        halt.set()

    monkeypatch.setattr(heart_fill, "write", stop_after_first)
    assert await backfill_heart_hours(pg_sessions, halt=halt) == 1

    async def broken(*args):
        raise RuntimeError("synthetic interrupted hour")

    monkeypatch.setattr(heart_fill, "write", broken)
    with pytest.raises(RuntimeError, match="interrupted"):
        await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        assert await session.scalar(select(func.count(HeartHour.samples))) == 1
        assert not (await session.get(HeartProjectionState, 1)).ready
    monkeypatch.setattr(heart_fill, "write", original)
    assert await backfill_heart_hours(pg_sessions, limit=2) == 2
    assert await backfill_heart_hours(pg_sessions) == len(await populated(pg_sessions)) - 3
    await assert_equal_to_per_hour(pg_sessions)


async def test_a_held_fence_is_polled_then_the_fill_stops_without_writing(pg_sessions, monkeypatch):
    await archive(pg_sessions, 5, names=10, mutations=0)
    await backfill_heart_hours(pg_sessions, limit=1)
    monkeypatch.setattr(heart_maintenance, "FENCE_TRIES", 2)
    monkeypatch.setattr(heart_maintenance, "FENCE_WAIT", 0)
    original = heart_fill.revisions

    async def writer_holds(session, start, stop):
        found = await original(session, start, stop)
        holder = pg_sessions()
        await holder.begin()
        await holder.execute(select(func.pg_advisory_xact_lock(heart_maintenance.HEART_LOCK)))
        held.append(holder)
        return found

    held = []
    monkeypatch.setattr(heart_fill, "revisions", writer_holds)
    assert await backfill_heart_hours(pg_sessions) == 0
    await held[0].rollback()
    await held[0].close()
    async with pg_sessions() as session:
        assert await session.scalar(select(func.count(HeartHour.samples))) == 1
