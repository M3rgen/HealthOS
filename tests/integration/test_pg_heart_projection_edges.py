"""Projection policy, exact sport boundaries and failure recovery on PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from derived_records import BAND, PHONE, T0, store
from sqlalchemy import and_, select, update
from test_heart_corrections import heart

from app.models.health import HeartRate
from app.models.heart_projection import HeartHour, HeartProjectionState
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.providers.ingestion_readers import GOOGLE_ARCHIVE
from app.services.readers import (
    current_heart,
    heart_maintenance,
    refresh_heart_hours,
)
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.heart_maintenance import backfill_heart_hours
from app.services.readers.reading import ArchiveReaders, HeartRead

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "at", [T0, datetime(2026, 10, 25, 0, tzinfo=UTC), datetime(2026, 10, 25, 1, tzinfo=UTC)]
)
async def test_sport_start_stop_inside_minute_preserves_every_selected_sample(pg_sessions, at):
    offsets = (0, 29, 30, 31, 49, 50, 59)
    await store(
        pg_sessions,
        "heart-rate",
        *[
            heart(str(i), at=at + timedelta(seconds=second), source=BAND, beatsPerMinute=60 + i)
            for i, second in enumerate(offsets)
        ],
    )

    async def sport(session, start, stop, predicate):
        return HeartRead(
            [(at + timedelta(seconds=s), 120 + i, "GARMIN") for i, s in enumerate(offsets)],
            "GARMIN",
            True,
            [(at + timedelta(seconds=30), at + timedelta(seconds=50))],
            "GARMIN",
        )

    query = DashboardQuery(at.date(), 7, "Europe/Brussels")
    archives = (GOOGLE_ARCHIVE, ArchiveReaders("synthetic-sport", heart_rate=sport))
    async with pg_sessions() as session:
        expected = await dashboard_data(session, query=query, archives=archives)
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        actual = await dashboard_data(session, query=query, archives=archives)
        assert actual == expected
        assert actual["charts"]["heart_rate"][2] == 88.7  # all seven winners, not slice means
        source = actual["charts"]["sources"]["heart_rate"]
        assert source["activity_windows"] == "GARMIN"
        assert set(source["minutes_by_source"]) == {"FITBIT", "GARMIN"}
        # Three winning Garmin samples inside [30,50); four Fitbit samples outside.
        assert 60 < max(v for v in actual["charts"]["heart_rate"] if v is not None) < 120


@pytest.mark.parametrize("platform", [None, {"unknown": [1, "x"]}, "x" * 33000])
async def test_platform_is_lossless_and_never_indexed_as_a_large_key(pg_sessions, platform):
    await store(
        pg_sessions, "heart-rate", heart(source={"platform": platform}, beatsPerMinute=71.23456789)
    )
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        [row] = list(await session.scalars(select(HeartHour)))
        raw = await session.scalar(select(RawHealthData))
        assert row.samples[0] == [
            str(raw.id),
            raw.source_start_time.isoformat(),
            T0.isoformat(),
            (71.23456789).hex(),
            platform,
        ]
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert await current_heart.chart_heart_rate_samples(
            session, T0, T0 + timedelta(hours=1), filt
        ) == [(T0, 71.23456789, platform)]


async def test_opaque_filter_keeps_all_additional_conditions(pg_sessions, monkeypatch):
    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        filt = and_(DashboardSources().normalized(HeartRate, "heart-rate"), HeartRate.bpm < 70)
        assert (
            await current_heart.chart_heart_rate_samples(session, T0, T0 + timedelta(days=1), filt)
            == []
        )


async def test_raw_bounds_win_over_parsed_and_normalized_times(pg_sessions):
    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    moved = T0 + timedelta(hours=1, minutes=30)
    async with pg_sessions.begin() as session:
        await session.execute(update(RawHealthData).values(source_start_time=moved))
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert await current_heart.chart_heart_rate_samples(session, T0, moved, filt) == []
        assert await current_heart.chart_heart_rate_samples(
            session, moved, moved + timedelta(microseconds=1), filt
        ) == [(T0, 77, "HEALTH_KIT")]


async def test_interrupted_builder_rolls_back_cursor_and_resumes(pg_sessions, monkeypatch):
    await store(
        pg_sessions,
        "heart-rate",
        heart(beatsPerMinute=77),
        heart("later", at=T0 + timedelta(hours=1), bpm=63),
    )
    assert await backfill_heart_hours(pg_sessions, limit=1) == 1
    original = heart_maintenance._pack

    async def broken(session, bucket):
        raise RuntimeError("synthetic interrupted hour")

    monkeypatch.setattr(heart_maintenance, "_pack", broken)
    with pytest.raises(RuntimeError, match="interrupted"):
        await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        state = await session.get(HeartProjectionState, 1)
        assert not state.ready and state.next_hour == T0 + timedelta(hours=1)
    monkeypatch.setattr(heart_maintenance, "_pack", original)
    assert await backfill_heart_hours(pg_sessions) == 1


async def test_only_affected_hour_is_reparsed_and_identical_refetch_stays_current(
    pg_sessions, monkeypatch
):
    records = [heart(str(i), at=T0 + timedelta(hours=i), beatsPerMinute=70 + i) for i in range(24)]
    await store(pg_sessions, "heart-rate", *records)
    await backfill_heart_hours(pg_sessions, limit=24)
    await store(pg_sessions, "heart-rate", *records)
    assert await refresh_heart_hours(pg_sessions) == 0
    await store(
        pg_sessions, "heart-rate", heart("5", at=T0 + timedelta(hours=5), source=PHONE, bpm=123)
    )
    parsed = []
    original = current_heart.current_heart_rate

    def tracked(record):
        parsed.append(record)
        return original(record)

    monkeypatch.setattr(current_heart, "current_heart_rate", tracked)
    assert await refresh_heart_hours(pg_sessions) == 1
    assert len(parsed) == 1
    async with pg_sessions() as session:
        await dashboard_data(session, query=DashboardQuery(T0.date(), 7, "UTC"))
    assert len(parsed) == 1


async def test_concurrent_first_readers_do_not_build_and_repair_cannot_overwrite(pg_sessions):
    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))

    async def read():
        async with pg_sessions() as session:
            return await current_heart.chart_heart_rate_samples(
                session,
                T0,
                T0 + timedelta(days=1),
                DashboardSources().normalized(HeartRate, "heart-rate"),
            )

    assert await asyncio.gather(read(), read()) == [[(T0, 77, "HEALTH_KIT")]] * 2
    async with pg_sessions() as session:
        assert await session.get(HeartProjectionState, 1) is None
    await asyncio.gather(backfill_heart_hours(pg_sessions), backfill_heart_hours(pg_sessions))
    await store(pg_sessions, "heart-rate", heart(at=T0, source=BAND, bpm=123))
    await asyncio.gather(refresh_heart_hours(pg_sessions), refresh_heart_hours(pg_sessions), read())
    assert await read() == [(T0, 123, "FITBIT")]


@pytest.mark.parametrize("statement", ["DELETE FROM heart_rate", "TRUNCATE heart_rate"])
async def test_normalized_bulk_delete_or_truncate_never_serves_a_retained_packet(
    pg_sessions, statement
):
    from sqlalchemy import text

    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions.begin() as session:
        await session.execute(text(statement))
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert (
            await current_heart.chart_heart_rate_samples(session, T0, T0 + timedelta(days=1), filt)
            == []
        )
    async with pg_sessions() as session:
        assert await session.scalar(select(RawHealthData.id)) is not None
    assert await refresh_heart_hours(pg_sessions) == 1


async def test_repeatable_read_never_builds_and_uses_snapshot_consistent_dirty_fallback(
    pg_sessions,
):
    from sqlalchemy import text

    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions.begin() as session:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        assert not await heart_maintenance.fence(session)
        await session.execute(update(RawHealthData).values(is_quarantined=True))
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert (
            await current_heart.chart_heart_rate_samples(session, T0, T0 + timedelta(days=1), filt)
            == []
        )


async def test_live_holder_deletion_revival_and_device_rollback_rebuild_old_hours(pg_sessions):
    from app.models.identity_tables import RawHealthDataSupersession, RawSourceDevice

    await store(
        pg_sessions,
        "heart-rate",
        heart("old", beatsPerMinute=60),
        heart("holder", at=T0 + timedelta(hours=1), beatsPerMinute=90),
    )
    async with pg_sessions.begin() as session:
        rows = {
            r.google_data_point_id.rsplit("/", 1)[-1]: r
            for r in await session.scalars(select(RawHealthData))
        }
        old, holder = rows["old"], rows["holder"]
        holder_id = holder.id
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old.id,
                new_raw_id=holder.id,
                data_type="heart-rate",
                old_google_data_point_id=old.google_data_point_id,
                new_google_data_point_id=holder.google_data_point_id,
                reason="duplicate",
                old_fingerprint="a" * 64,
                new_fingerprint="b" * 64,
                same_reading=False,
                new_payload_hash=holder.payload_hash,
            )
        )
    await backfill_heart_hours(pg_sessions)

    async def values(deleted=False):
        async with pg_sessions() as session:
            filt = DashboardSources(deleted).normalized(HeartRate, "heart-rate")
            projected = await current_heart.chart_heart_rate_samples(
                session, T0, T0 + timedelta(days=1), filt
            )
            assert projected == await current_heart.heart_rate_samples(
                session, T0, T0 + timedelta(days=1), filt
            )
            return [v for t, v, p in projected]

    assert await values() == [60, 90]
    async with pg_sessions.begin() as session:
        session.add_all(
            [
                RawSourceDevice(
                    data_type="heart-rate", fingerprint=fp * 64, device="a" * 64, decided_at=T0
                )
                for fp in ("a", "b")
            ]
        )
    assert await values() == [90]
    await refresh_heart_hours(pg_sessions)
    for field, off in (("is_quarantined", True), ("source_deleted_at", T0)):
        async with pg_sessions.begin() as session:
            await session.execute(
                update(RawHealthData).where(RawHealthData.id == holder_id).values(**{field: off})
            )
        assert await values() == [60]
        assert await values(deleted=True) == [60]
        await refresh_heart_hours(pg_sessions)
        async with pg_sessions.begin() as session:
            await session.execute(
                update(RawHealthData)
                .where(RawHealthData.id == holder_id)
                .values(**{field: False if field == "is_quarantined" else None})
            )
        assert await values() == [90]
        await refresh_heart_hours(pg_sessions)
    async with pg_sessions.begin() as session:
        session.add(
            RawSourceDevice(
                data_type="heart-rate", fingerprint="b" * 64, device="b" * 64, decided_at=T0
            )
        )
    assert await values() == [60, 90]
    await refresh_heart_hours(pg_sessions)
    assert await values() == [60, 90]


async def test_normalized_key_rewrite_invalidates_old_membership(pg_sessions):
    import uuid

    await store(pg_sessions, "heart-rate", heart(beatsPerMinute=77))
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions.begin() as session:
        await session.execute(
            update(HeartRate).values(id=uuid.uuid4(), google_data_point_id="synthetic/orphan")
        )
    async with pg_sessions() as session:
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert (
            await current_heart.chart_heart_rate_samples(session, T0, T0 + timedelta(days=1), filt)
            == []
        )
    assert await refresh_heart_hours(pg_sessions) == 1


async def test_failed_hour_is_rolled_back_without_blocking_other_repairs(pg_sessions, monkeypatch):
    await store(
        pg_sessions,
        "heart-rate",
        heart(beatsPerMinute=77),
        heart("later", at=T0 + timedelta(hours=1), bpm=63),
    )
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions.begin() as session:
        await session.execute(update(RawHealthData).values(is_quarantined=True))
    original = heart_maintenance._pack

    async def broken(session, bucket):
        if bucket == T0:
            raise ValueError("synthetic failed hour")
        return await original(session, bucket)

    monkeypatch.setattr(heart_maintenance, "_pack", broken)
    assert await refresh_heart_hours(pg_sessions) == 1
    async with pg_sessions() as session:
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert (
            await current_heart.chart_heart_rate_samples(session, T0, T0 + timedelta(days=1), filt)
            == []
        )
    monkeypatch.setattr(heart_maintenance, "_pack", original)
    assert await refresh_heart_hours(pg_sessions) == 1


async def test_history_restore_and_promote_invalidate_without_normalized_value_fallback(
    pg_sessions,
):
    import json

    from app.parsers.common import payload_hash
    from app.services.archive.raw_history_types import Candidate
    from app.services.archive.raw_history_writing import promote, restore_record

    def candidate(at, value):
        body = heart(at=at, beatsPerMinute=value)
        return Candidate(
            data_type="heart-rate",
            google_id=body["name"],
            raw_text=json.dumps(body),
            payload_hash=payload_hash(body),
            source_start_time=at,
            source_end_time=None,
            source_timezone=None,
            received_at=T0,
            last_seen_at=T0,
            superseded_at=None,
            value=body,
        )

    assert await backfill_heart_hours(pg_sessions) == 0  # empty horizon is ready
    async with pg_sessions.begin() as session:
        restored = await restore_record(session, candidate(T0, 77))
        assert restored is not None
        raw_id = restored.raw_id
        session.add(HeartRate(google_data_point_id=heart()["name"], timestamp=T0, bpm=77))
    async with pg_sessions.begin() as session:
        filt = DashboardSources().normalized(HeartRate, "heart-rate")
        assert (
            await current_heart.chart_heart_rate_samples(session, T0, T0 + timedelta(days=1), filt)
            == []
        )
        await session.execute(update(RawHealthData).values(source_deleted_at=None))
    assert await refresh_heart_hours(pg_sessions) == 1
    for at, value, expected in (
        (T0 + timedelta(hours=1), "-1e1000", []),
        (T0 + timedelta(hours=2), 63.123456, [(T0 + timedelta(hours=2), 63.123456, "HEALTH_KIT")]),
    ):
        async with pg_sessions.begin() as session:
            await promote(session, raw_id, candidate(at, value))
        async with pg_sessions() as session:
            filt = DashboardSources().normalized(HeartRate, "heart-rate")
            assert (
                await current_heart.chart_heart_rate_samples(
                    session, T0, T0 + timedelta(days=1), filt
                )
                == expected
            )
            assert await session.scalar(select(HeartRate.bpm)) == 77
        await refresh_heart_hours(pg_sessions)


async def test_both_repeated_dst_hours_merge_into_existing_twenty_four_slots(pg_sessions):
    at = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    await store(
        pg_sessions,
        "heart-rate",
        heart("first-fold", at=at, source=BAND, bpm=60),
        heart("second-fold", at=at + timedelta(hours=1), source=BAND, bpm=100),
    )
    query = DashboardQuery(at.date(), 7, "Europe/Brussels")
    async with pg_sessions() as session:
        expected = await dashboard_data(session, query=query)
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        result = await dashboard_data(session, query=query)
        assert result == expected
        assert len(result["charts"]["heart_rate"]) == 24
        assert result["charts"]["heart_rate"][2] == 80
        assert len(list(await session.scalars(select(HeartHour)))) == 2


async def test_aliased_model_predicate_keeps_its_original_sql_semantics(pg_sessions):
    from sqlalchemy.orm import aliased

    await store(
        pg_sessions,
        "heart-rate",
        heart("one", bpm=60),
        heart("two", at=T0 + timedelta(seconds=30), bpm=90),
    )
    await backfill_heart_hours(pg_sessions)
    async with pg_sessions() as session:
        predicate = DashboardSources().normalized(aliased(HeartRate), "heart-rate")
        raw = await current_heart.heart_rate_samples(session, T0, T0 + timedelta(days=1), predicate)
        # This caller supplied an uncorrelated alias: preserve even its multiplicity.
        assert len(raw) == 4
        assert (
            await current_heart.chart_heart_rate_samples(
                session, T0, T0 + timedelta(days=1), predicate
            )
            == raw
        )
