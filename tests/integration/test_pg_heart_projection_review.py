"""Reviewer's seeded differential test: projected reader == raw reader after random writes."""

import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import point
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.google_health.data_types import REGISTRY
from app.models.health import HeartRate
from app.models.identity_tables import RawHealthDataSupersession, RawSourceDevice
from app.models.state import RawHealthData
from app.services.archive.storage import store_records
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.readers import current_heart, heart_projection, refresh_heart_hours
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.heart_maintenance import backfill_heart_hours

pytestmark = pytest.mark.integration
BASE = datetime(2026, 10, 24, tzinfo=UTC)
HOURS = 96
ZONES = (
    "UTC",
    "Europe/Berlin",
    "Asia/Kolkata",
    "Asia/Kathmandu",
    "America/St_Johns",
    "Australia/Lord_Howe",
    "Pacific/Chatham",
)
PLATFORMS = (
    {"platform": "FITBIT", "device": {"displayName": "band"}},
    {"platform": "HEALTH_KIT", "device": {"formFactor": "PHONE"}},
    {"platform": "HEALTH_CONNECT"},
    {"platform": "UNSEEN_PLATFORM_ZZ"},
    None,
)
VALUES = (61, 72.5, 88, 95.25, 101, 64, 0, "-0", -0.0, "1e-320", -1, "-1e-30", "NaN", None, True)
VALUES += ("7e400", 180.123456)


def moment(rng):
    hour = BASE + timedelta(hours=rng.randrange(HOURS))
    offset = rng.choice(
        (
            timedelta(0),
            timedelta(microseconds=1),
            timedelta(seconds=1),
            timedelta(minutes=30),
            timedelta(minutes=45),
            timedelta(minutes=59, seconds=59, microseconds=999999),
            timedelta(seconds=rng.randrange(3600)),
        )
    )
    return hour + offset


def record(rng, name):
    at = moment(rng)
    zone = rng.choice(("+00:00", "+05:30", "-03:30", "+05:45", "Z"))
    stamp = at.astimezone(
        UTC if zone in ("Z", "+00:00") else datetime.fromisoformat("2000-01-01T00:00" + zone).tzinfo
    ).isoformat()
    if zone == "Z":
        stamp = stamp.replace("+00:00", "Z")
    alias = rng.choice(("beatsPerMinute", "bpm", "value"))
    payload = {"sampleTime": {"physicalTime": stamp}, alias: rng.choice(VALUES)}
    source = rng.choice(PLATFORMS)
    found = point("heart-rate", name, payload, source or {})
    if source is None:
        found.pop("dataSource")
    return found


async def store(sessions, records):
    async with sessions() as session, session.begin():
        await store_records(session, REGISTRY["heart-rate"], records)


async def ids(sessions):
    async with sessions() as session:
        return list(
            (
                await session.execute(
                    select(RawHealthData.id, RawHealthData.google_data_point_id).where(
                        RawHealthData.data_type == "heart-rate"
                    )
                )
            ).all()
        )


class _Rollback(Exception):
    pass


async def mutate(sessions, rng, rollback):
    rows = await ids(sessions)
    if not rows:
        return
    raw_id, key = rng.choice(rows)
    kind = rng.choice(
        ("quarantine", "release", "normq", "normr", "delete", "revive", "link", "device", "holder")
    )
    async with sessions() as session:
        try:
            async with session.begin():
                if kind in ("quarantine", "release"):
                    await session.execute(
                        update(RawHealthData)
                        .where(RawHealthData.id == raw_id)
                        .values(is_quarantined=kind == "quarantine")
                    )
                elif kind in ("normq", "normr"):
                    await session.execute(
                        update(HeartRate)
                        .where(HeartRate.google_data_point_id == key)
                        .values(is_quarantined=kind == "normq")
                    )
                elif kind in ("delete", "revive"):
                    await session.execute(
                        update(RawHealthData)
                        .where(RawHealthData.id == raw_id)
                        .values(source_deleted_at=BASE if kind == "delete" else None)
                    )
                elif kind == "link":
                    other_id, other_key = rng.choice(rows)
                    if other_id == raw_id:
                        return
                    old_fp, new_fp = rng.choice(("a", "b")) * 64, rng.choice(("a", "b")) * 64
                    session.add(
                        RawHealthDataSupersession(
                            old_raw_id=raw_id,
                            new_raw_id=other_id,
                            data_type="heart-rate",
                            old_google_data_point_id=key,
                            new_google_data_point_id=other_key,
                            reason="duplicate",
                            old_fingerprint=old_fp,
                            new_fingerprint=new_fp,
                            same_reading=True,
                            new_payload_hash="0" * 64,
                        )
                    )
                elif kind == "device":
                    session.add(
                        RawSourceDevice(
                            data_type="heart-rate",
                            fingerprint=rng.choice(("a", "b")) * 64,
                            device=rng.choice(("c", "d")) * 64,
                            decided_at=BASE,
                        )
                    )
                else:  # holder deletion/quarantine through the held row's link
                    await session.execute(
                        text(
                            "UPDATE raw_health_data SET source_deleted_at = "
                            "CASE WHEN source_deleted_at IS NULL THEN :t ELSE NULL END "
                            "WHERE id IN (SELECT new_raw_id FROM raw_health_data_supersessions "
                            "WHERE old_raw_id = :r)"
                        ),
                        {"t": BASE, "r": raw_id},
                    )
                await session.flush()
                if rollback:
                    raise _Rollback
        except _Rollback:
            pass
        except (IntegrityError, DBAPIError):
            pass


def exact(samples):
    return sorted((moment, bpm.hex(), repr(platform)) for moment, bpm, platform in samples)


async def compare(sessions, rng, monkeypatch):
    predicate = DashboardSources().normalized(HeartRate, "heart-rate")
    for _ in range(4):
        start = BASE + timedelta(minutes=rng.randrange(0, HOURS * 60, 15))
        stop = start + timedelta(minutes=rng.randrange(15, 3 * 24 * 60, 15))
        async with sessions() as session:
            raw = await current_heart.heart_rate_samples(session, start, stop, predicate)
            projected = await current_heart.chart_heart_rate_samples(
                session, start, stop, predicate
            )
        # Both readers order by raw source time only; equal-time samples have no defined
        # order (the raw plan may change it), so compare exact values as multisets.
        assert exact(projected) == exact(raw), (start, stop)
    zone = rng.choice(ZONES)
    day = (BASE + timedelta(days=rng.randrange(1, 4))).astimezone(ZoneInfo(zone)).date()
    query = DashboardQuery(day, rng.choice((1, 2, 7)), zone)
    async with sessions() as session:
        projected = await dashboard_data(session, query=query)
    with monkeypatch.context() as patch:

        async def none(*args, **kwargs):
            return None

        patch.setattr(heart_projection, "packets", none)
        async with sessions() as session:
            raw = await dashboard_data(session, query=query)
    assert projected == raw, (zone, day)


@pytest.mark.parametrize("seed", range(16))
async def test_random_writes_keep_projection_equal_to_raw(pg_sessions, seed, monkeypatch):
    rng = random.Random(seed)
    names = [f"r{i}" for i in range(40)]
    await store(pg_sessions, [record(rng, name) for name in names[:20]])
    for step in range(45):
        action = rng.random()
        if action < 0.3:
            batch = rng.sample(names, rng.randrange(1, 8))
            await store(pg_sessions, [record(rng, name) for name in batch])
        elif action < 0.6:
            await mutate(pg_sessions, rng, rollback=rng.random() < 0.2)
        elif action < 0.75:
            await backfill_heart_hours(pg_sessions, limit=rng.randrange(1, 60))
        else:
            await refresh_heart_hours(pg_sessions, limit=rng.randrange(1, 17))
        if step % 5 == 4:
            await compare(pg_sessions, rng, monkeypatch)
    # Drain completely so the comparison also covers fully warm packets.
    while await backfill_heart_hours(pg_sessions, limit=168):
        pass
    while await refresh_heart_hours(pg_sessions, limit=168):
        pass
    async with pg_sessions() as session:
        dirty = await session.scalar(
            text("SELECT count(*) FROM google_heart_hours WHERE revision <> built_revision")
        )
    assert dirty == 0
    for _ in range(3):
        await compare(pg_sessions, rng, monkeypatch)
