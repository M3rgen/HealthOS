"""Original-sample projection contract, against real migrations and archive writers."""

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import product
from zoneinfo import ZoneInfo

import pytest
from derived_records import BAND, PHONE, T0, point, raw_mutation, snapshot, steps, store
from sqlalchemy import func, select, text, update
from test_heart_corrections import heart

from app.models.health import HeartRate
from app.models.heart_projection import HeartHour, HeartPending, HeartProjectionState
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard import dashboard_data, hourly_means
from app.services.readers import (
    current_heart,
    heart_projection,
    refresh_heart_hours,
)
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.heart_maintenance import backfill_heart_hours

pytestmark = pytest.mark.integration
STOP = T0 + timedelta(days=1)


async def fill(sessions, limit=48):
    return await backfill_heart_hours(sessions, limit=limit)


async def samples(sessions, start=T0, stop=STOP, deleted=False):
    async with sessions() as session:
        predicate = DashboardSources(deleted).normalized(HeartRate, "heart-rate")
        raw = await current_heart.heart_rate_samples(session, start, stop, predicate)
        projected = await current_heart.chart_heart_rate_samples(session, start, stop, predicate)
        assert projected == raw
        return projected


async def test_initialization_is_bounded_resumable_and_preserves_archive(pg_sessions):
    await store(
        pg_sessions, "heart-rate", heart(beatsPerMinute=77), heart("later", at=STOP, bpm=53)
    )
    async with pg_sessions() as session:
        before = await snapshot(session, heart_projection.TABLES)
    assert await fill(pg_sessions, 1) == 1
    async with pg_sessions() as session:
        state = await session.get(HeartProjectionState, 1)
        assert not state.ready
    assert len(await samples(pg_sessions)) == 1  # incomplete initialization uses raw
    # The 23 empty hours between the two samples need no packet; the cursor skips them.
    assert await fill(pg_sessions, 24) == 1
    async with pg_sessions() as session:
        assert (await session.get(HeartProjectionState, 1)).ready
        assert await snapshot(session, heart_projection.TABLES) == before
    assert await fill(pg_sessions, 1) == 0
    assert len(await samples(pg_sessions)) == 1


@pytest.mark.parametrize("glitch", ["1970-01-01T00:00:00Z", "9999-12-31T23:30:00Z"])
async def test_an_outlier_timestamp_neither_stretches_nor_breaks_the_fill(pg_sessions, glitch):
    await store(
        pg_sessions,
        "heart-rate",
        heart(beatsPerMinute=77),
        point("heart-rate", "glitch", {"sampleTime": {"physicalTime": glitch}, "bpm": 64}, BAND),
    )
    # Two populated hours, decades apart: one bounded call completes the horizon.
    assert await fill(pg_sessions, 3) == 2
    async with pg_sessions() as session:
        assert (await session.get(HeartProjectionState, 1)).ready
        assert await session.scalar(select(func.count()).select_from(HeartHour)) == 2
    assert len(await samples(pg_sessions)) == 1
    if glitch.startswith("1970"):
        outlier = datetime.fromisoformat(glitch)
        found = await samples(pg_sessions, outlier, outlier + timedelta(hours=1))
        assert [bpm for _, bpm, _ in found] == [64]
    # Empty hours have no packet and read the (empty) raw hour.
    assert await samples(pg_sessions, T0 + timedelta(hours=3), T0 + timedelta(hours=9)) == []


@pytest.mark.parametrize(
    "payload,alias",
    product(("heartRate", "heart_rate", "heart-rate"), ("beatsPerMinute", "bpm", "value")),
)
async def test_validator_aliases_and_numeric_boundaries(pg_sessions, payload, alias):
    values = [
        None,
        True,
        "NaN",
        "Infinity",
        "-Infinity",
        "-1e-1000",
        "-1e1000",
        "1e1000",
        "1e-1000",
        "-0",
        "0",
        "0.00049",
        "1.7976931348623157e308",
        "2.2250738585072014e-308",
        "4.9406564584124654e-324",
        71.23456789,
    ]
    await store(
        pg_sessions,
        "heart-rate",
        *(heart(str(i), beatsPerMinute=70) for i in range(len(values) + 1)),
    )
    records = []
    for i, value in enumerate(values):
        record = heart(str(i), at=T0 + timedelta(seconds=i), source=PHONE)
        record[payload] = {**record.pop("heartRate"), alias: value}
        records.append(record)
    records.append(heart(str(len(values))))  # absent
    await store(pg_sessions, "heart-rate", *records)
    await fill(pg_sessions)
    found = await samples(pg_sessions)
    assert [bpm.hex() for _, bpm, _ in found] == [
        parsed[1].hex()
        for record in records
        if (parsed := current_heart.current_heart_rate(record)) is not None
    ]


async def test_invalid_correction_moves_raw_hour_and_never_borrows_normalized(pg_sessions):
    await store(pg_sessions, "heart-rate", heart(source=BAND, beatsPerMinute=77))
    await fill(pg_sessions)
    await store(
        pg_sessions, "heart-rate", heart(at=STOP, source=PHONE, beatsPerMinute="-1e1000", bpm=140)
    )
    assert await samples(pg_sessions) == []
    assert await samples(pg_sessions, STOP, STOP + timedelta(days=1)) == []
    async with pg_sessions() as session:
        assert await session.scalar(select(HeartRate.bpm)) == 77
        hours = list(await session.scalars(select(HeartHour).order_by(HeartHour.bucket)))
        assert hours[0].revision != hours[0].built_revision
    await refresh_heart_hours(pg_sessions)
    await store(pg_sessions, "heart-rate", heart(at=STOP, source=BAND, value=63.123456))
    await refresh_heart_hours(pg_sessions)
    assert await samples(pg_sessions, STOP, STOP + timedelta(days=1)) == [
        (STOP, 63.123456, "FITBIT")
    ]


@pytest.mark.parametrize(
    "operation", ["move", "delete", "revive", "supersession", "device", "payload", "quarantine"]
)
async def test_direct_sql_identity_and_archive_invalidation_rollback(pg_sessions, operation):
    mutation = await raw_mutation(pg_sessions, "heart-rate", operation)
    await fill(pg_sessions)
    async with pg_sessions() as session:
        before = dict((await session.execute(select(HeartHour.bucket, HeartHour.revision))).all())
    async with pg_sessions() as session:
        await session.execute(mutation)
        during = dict((await session.execute(select(HeartHour.bucket, HeartHour.revision))).all())
        assert during != before
        await session.rollback()
    async with pg_sessions() as session:
        assert (
            dict((await session.execute(select(HeartHour.bucket, HeartHour.revision))).all())
            == before
        )
    async with pg_sessions.begin() as session:
        await session.execute(mutation)
    async with pg_sessions() as session:
        assert (
            dict((await session.execute(select(HeartHour.bucket, HeartHour.revision))).all())
            != before
        )
    await samples(pg_sessions)


@pytest.mark.parametrize(
    "model,values",
    [
        (RawHealthData, {"is_quarantined": True}),
        (HeartRate, {"is_quarantined": True}),
        (RawHealthData, {"source_deleted_at": T0}),
    ],
)
async def test_eligibility_and_deletion_modes(pg_sessions, model, values):
    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await fill(pg_sessions)
    async with pg_sessions.begin() as session:
        await session.execute(update(model).values(**values))
        assert (
            await current_heart.chart_heart_rate_samples(
                session, T0, STOP, DashboardSources().normalized(HeartRate, "heart-rate")
            )
            == []
        )
    assert await samples(pg_sessions) == []
    assert len(await samples(pg_sessions, deleted=True)) == int("source_deleted_at" in values)
    await refresh_heart_hours(pg_sessions)
    assert await samples(pg_sessions) == []
    async with pg_sessions.begin() as session:
        await session.execute(
            update(model).values(
                **{key: None if key == "source_deleted_at" else False for key in values}
            )
        )
    await refresh_heart_hours(pg_sessions)
    assert len(await samples(pg_sessions)) == 1


@pytest.mark.parametrize("damage", ["missing", "method", "revision", "hash", "body"])
async def test_corrupt_or_missing_hour_falls_back_without_omitting_samples(pg_sessions, damage):
    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await fill(pg_sessions)
    async with pg_sessions.begin() as session:
        if damage == "missing":
            await session.execute(
                text("DELETE FROM google_heart_hours WHERE bucket = :t"), {"t": T0}
            )
        else:
            values = (
                {"method": "old"}
                if damage == "method"
                else {"built_revision": 0}
                if damage == "revision"
                else {"digest": "bad"}
                if damage == "hash"
                else {"samples": [["bad"]]}
            )
            await session.execute(update(HeartHour).where(HeartHour.bucket == T0).values(**values))
    assert await samples(pg_sessions) == [(T0, 77, "HEALTH_KIT")]


@pytest.mark.parametrize(
    "zone,day",
    [
        ("Asia/Kolkata", "2026-09-29"),
        ("Europe/Brussels", "2026-03-29"),
        ("Europe/Brussels", "2026-10-25"),
        ("America/New_York", "2026-11-01"),
    ],
)
async def test_partial_utc_hours_dst_and_dashboard_equality(pg_sessions, zone, day, monkeypatch):
    first = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
    last = (
        (datetime.fromisoformat(day) + timedelta(days=1))
        .replace(tzinfo=ZoneInfo(zone))
        .astimezone(UTC)
    )
    moments = [
        first - timedelta(microseconds=1),
        first,
        first + timedelta(seconds=30),
        last - timedelta(microseconds=1),
        last,
    ]
    await store(
        pg_sessions,
        "heart-rate",
        *(
            heart(str(i), at=at, source=BAND if i % 2 else PHONE, beatsPerMinute=60 + i)
            for i, at in enumerate(moments)
        ),
    )
    query = DashboardQuery(first.astimezone(ZoneInfo(zone)).date(), 7, zone)
    async with pg_sessions() as session:
        expected = await dashboard_data(session, query=query)
    await fill(pg_sessions, 48)
    found = await samples(pg_sessions, first, last)
    assert len(found) == 3
    assert len(hourly_means([(t, v) for t, v, _ in found], ZoneInfo(zone))) == 24
    async with pg_sessions() as session:
        assert await dashboard_data(session, query=query) == expected

    # A prepared reader must not reparse any current payload.
    def forbidden(record):
        raise AssertionError("warm projection decoded raw payload")

    monkeypatch.setattr(current_heart, "current_heart_rate", forbidden)
    async with pg_sessions() as session:
        assert await dashboard_data(session, query=query) == expected


async def revisions(sessions):
    async with sessions() as session:
        pending = await session.scalar(select(func.count()).select_from(HeartPending))
        hours = dict((await session.execute(select(HeartHour.bucket, HeartHour.revision))).all())
        return hours, pending


async def test_a_page_bumps_each_hour_once_and_other_types_touch_nothing(pg_sessions):
    later = T0 + timedelta(hours=1)
    await store(pg_sessions, "heart-rate", heart("a", at=T0), heart("b", at=later))
    await fill(pg_sessions)
    before, _ = await revisions(pg_sessions)
    await store(
        pg_sessions,
        "heart-rate",
        *(heart(f"p{i}", at=T0 + timedelta(seconds=37 * i), beatsPerMinute=60) for i in range(150)),
    )
    after, pending = await revisions(pg_sessions)
    # 150 rows over two hours, plus their normalized rows: one bump per hour, nothing left.
    assert {bucket: after[bucket] - before[bucket] for bucket in before} == {T0: 1, later: 1}
    assert pending == 0
    await store(pg_sessions, "steps", *(steps(f"s{i}", start=T0) for i in range(20)))
    assert await revisions(pg_sessions) == (after, 0)


async def test_a_rolled_back_savepoint_takes_its_bump_and_dedupe_with_it(pg_sessions):
    await store(
        pg_sessions,
        "heart-rate",
        heart("a", beatsPerMinute=61),
        heart("b", at=T0 + timedelta(minutes=5), beatsPerMinute=62),
    )
    await fill(pg_sessions)
    before, _ = await revisions(pg_sessions)
    quarantine = update(RawHealthData).values(is_quarantined=True)
    async with pg_sessions.begin() as session:
        nested = await session.begin_nested()
        await session.execute(quarantine.where(RawHealthData.google_data_point_id.like("%/a")))
        assert (await session.get(HeartHour, T0, populate_existing=True)).revision == before[T0] + 1
        await nested.rollback()
        assert (await session.get(HeartHour, T0, populate_existing=True)).revision == before[T0]
        # The rolled-back pending row must not suppress this later change's bump.
        await session.execute(quarantine.where(RawHealthData.google_data_point_id.like("%/b")))
    assert await revisions(pg_sessions) == ({T0: before[T0] + 1}, 0)
    assert len(await samples(pg_sessions)) == 1
    await refresh_heart_hours(pg_sessions)
    assert len(await samples(pg_sessions)) == 1


async def test_repair_starts_with_the_newest_dirty_hour(pg_sessions):
    later = T0 + timedelta(hours=5)
    await store(pg_sessions, "heart-rate", heart("a", beatsPerMinute=61), heart("b", at=later))
    await fill(pg_sessions)
    async with pg_sessions.begin() as session:
        await session.execute(update(RawHealthData).values(is_quarantined=True))
    assert await refresh_heart_hours(pg_sessions, limit=1) == 1
    async with pg_sessions() as session:
        rows = {row.bucket: row for row in await session.scalars(select(HeartHour))}
    # The dashboard reads recent hours; an old backlog must not hold them back.
    assert rows[later].built_revision == rows[later].revision
    assert rows[T0].built_revision != rows[T0].revision


async def test_read_backfill_and_correction_are_fenced(pg_sessions):
    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await fill(pg_sessions)
    async with pg_sessions.begin() as writer:
        await writer.execute(update(RawHealthData).values(is_quarantined=True))
        # Builder never queues behind a writer and cannot publish an old revision.
        assert await refresh_heart_hours(pg_sessions) == 0
        # A reader sees the committed snapshot while this writer is uncommitted.
        assert await samples(pg_sessions) == [(T0, 77, "HEALTH_KIT")]
    await asyncio.gather(refresh_heart_hours(pg_sessions), refresh_heart_hours(pg_sessions))
    assert await samples(pg_sessions) == []
