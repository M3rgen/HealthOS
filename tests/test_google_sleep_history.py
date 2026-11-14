"""Sleep history follows current production-ingested evidence and daily estimates."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, PHONE, T0, sleep, snapshot, store
from sqlalchemy import select, update
from support.numeric_outputs import SLEEP_FIELDS, evidence

from app.core.config import get_settings
from app.models.health import SleepSession
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard_history import HistoryPageNotFound, history_data
from app.services.interpretation import google_daily_readers as reader
from app.services.readers import DashboardSources, daily_window
from app.services.readers.reading import ArchiveBounds

OLD, MIDDLE = DAY - timedelta(days=65), DAY - timedelta(days=3)
ZONE = ZoneInfo("UTC")


def night(name, day, **payload):
    start = T0 + timedelta(days=(day - DAY).days, hours=1)
    return sleep(name, start=start, end=start + timedelta(hours=8), **payload)


@pytest.mark.parametrize("field", SLEEP_FIELDS)
@pytest.mark.parametrize("invalid", ["-1", "NaN", "1e309", "-1e-400"])
@pytest.mark.parametrize("middle", [False, True])
async def test_correction_removes_both_archive_edges_and_recovery_restores_them(
    sessions, field, invalid, middle
):
    originals = [night(name, day) for name, day in (("old", OLD), ("new", DAY))]
    await store(sessions, "sleep", *originals, night("future", DAY + timedelta(days=1)))
    if middle:
        await store(sessions, "sleep", night("middle", MIDDLE))
    for corrected in (False, True, False):
        records = (
            [
                night(name, day, **evidence(field, invalid, T0 + timedelta(days=(day - DAY).days)))
                for name, day in (("old", OLD), ("new", DAY))
            ]
            if corrected
            else originals
        )
        await store(sessions, "sleep", *records)
        first = (MIDDLE if middle else None) if corrected else OLD
        last = (MIDDLE if middle else None) if corrected else DAY
        async with sessions.begin() as session:
            before = await snapshot(session)
            page = await history_data(session, "sleep", 1, "UTC", DAY)
            assert (page["earliest_date"], page["latest_date"]) == (first, last)
            pages = (DAY - first).days // 30 + 1 if first else 1
            assert page["total_pages"] == pages
            with pytest.raises(HistoryPageNotFound):
                await history_data(session, "sleep", pages + 1, "UTC", DAY)
            reads = {}
            await reader.read_sleep(
                session, daily_window(DAY, 66, ZONE), ZONE, DashboardSources(), reads
            )
            for day in (OLD, DAY):
                assert reads["FITBIT"].days[day]["sleep_seconds"].value == (
                    None if corrected else 28800
                )
            assert await snapshot(session) == before


@pytest.mark.parametrize("alternative", [None, 0, "unknown"])
@pytest.mark.parametrize("batch", [1, 3])
async def test_invalid_session_poisoning_is_per_day_and_platform(
    sessions, monkeypatch, alternative, batch
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", batch)
    # Even a preferred platform with a sound overlapping session loses its day
    # when another session is invalid. Other platforms still support the bounds.
    monkeypatch.setattr(get_settings(), "source_precedence_health", "FITBIT,HEALTH_KIT")
    await store(sessions, "sleep", night("valid", OLD), night("bad", OLD))
    await store(sessions, "sleep", night("bad", OLD, duration="-1s"))
    if alternative is not None:
        await store(
            sessions,
            "sleep",
            night(
                "phone",
                OLD,
                source=PHONE,
                **({"summary": {"minutesAsleep": 0}} if alternative == 0 else {}),
            ),
        )
    async with sessions() as session:
        bounds = await reader.archive_bounds(session, "sleep", DAY, ZONE)
        assert bounds == (ArchiveBounds() if alternative is None else ArchiveBounds(OLD, OLD))
        page = await history_data(session, "sleep", 3 if alternative is not None else 1, "UTC", DAY)
        measured = [row["value"] for row in page["days"] if row["value"] is not None]
        assert measured == ([] if alternative is None else [0 if alternative == 0 else 28800])


@pytest.mark.parametrize("state", ["deleted", "raw_quarantine", "normalized_quarantine", "held"])
async def test_bounds_share_daily_identity_and_eligibility(sessions, state):
    await store(sessions, "sleep", night("old", OLD), night("middle", MIDDLE))
    async with sessions.begin() as session:
        old, new = (
            await session.scalars(select(RawHealthData).order_by(RawHealthData.source_start_time))
        ).all()
        if state == "deleted":
            old.source_deleted_at = T0
        elif state == "raw_quarantine":
            old.is_quarantined = True
        elif state == "normalized_quarantine":
            await session.execute(
                update(SleepSession)
                .where(SleepSession.google_data_point_id == old.google_data_point_id)
                .values(is_quarantined=True)
            )
        else:
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=old.id,
                    new_raw_id=new.id,
                    data_type="sleep",
                    old_google_data_point_id=old.google_data_point_id,
                    new_google_data_point_id=new.google_data_point_id,
                    reason="duplicate",
                    old_fingerprint="a" * 64,
                    new_fingerprint="a" * 64,
                    same_reading=True,
                    new_payload_hash=new.payload_hash,
                )
            )
    async with sessions() as session:
        before = await snapshot(session)
        assert await reader.archive_bounds(session, "sleep", DAY, ZONE) == ArchiveBounds(
            MIDDLE, MIDDLE
        )
        reads = {}
        await reader.read_sleep(
            session, daily_window(DAY, 66, ZONE), ZONE, DashboardSources(), reads
        )
        assert set(reads["FITBIT"].days) == {MIDDLE}
        assert await snapshot(session) == before
