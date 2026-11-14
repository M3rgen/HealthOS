"""Heart history bounds follow current Google candidates through production ingestion."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, PHONE, T0, snapshot, store, summary
from garmin_records import daily_summary, store_page
from sqlalchemy import select, update

from app.core.config import get_settings
from app.models.health import RestingHeartRate
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard_history import HistoryPageNotFound, history_data
from app.services.interpretation import google_daily_readers as reader
from app.services.providers.garmin_normalize import normalize_window
from app.services.readers.reading import ArchiveBounds

KIND = "daily-resting-heart-rate"
OLD, MIDDLE = DAY - timedelta(days=65), DAY - timedelta(days=3)


async def history(sessions, earliest, latest, value, timezone="UTC"):
    async with sessions.begin() as session:
        before = await snapshot(session)
        page = await history_data(session, "heart", 1, timezone, DAY)
        assert page["earliest_date"] == earliest and page["latest_date"] == latest
        pages = (DAY - earliest).days // 30 + 1 if earliest else 1
        assert page["total_pages"] == pages
        assert next(row["value"] for row in page["days"] if row["date"] == str(MIDDLE)) == value
        if earliest == OLD:
            tail = await history_data(session, "heart", 3, timezone, DAY)
            assert any(row["date"] == str(OLD) and row["value"] is not None for row in tail["days"])
        with pytest.raises(HistoryPageNotFound):
            await history_data(session, "heart", pages + 1, timezone, DAY)
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize("middle", [False, True])
@pytest.mark.parametrize("timezone", ["UTC", "America/Los_Angeles", "Europe/Istanbul"])
@pytest.mark.parametrize(
    "invalid", [-1, "-1e-999999", "NaN", "Infinity", "1e999999", "1e-999999", "text", None, True]
)
async def test_invalid_old_and_new_days_do_not_expand_history(sessions, invalid, timezone, middle):
    await store(
        sessions,
        KIND,
        summary(KIND, "old", OLD, beatsPerMinute=invalid),
        summary(KIND, "new", DAY, beatsPerMinute=invalid),
        summary(KIND, "future", DAY + timedelta(days=1), beatsPerMinute=70),
    )
    if middle:
        await store(sessions, KIND, summary(KIND, "middle", MIDDLE, beatsPerMinute=60))
    await history(
        sessions,
        MIDDLE if middle else None,
        MIDDLE if middle else None,
        60 if middle else None,
        timezone,
    )


@pytest.mark.parametrize("zero", [0, "0", "-0", "-0e999999"])
async def test_measured_zero_establishes_bounds(sessions, zero):
    await store(
        sessions,
        KIND,
        summary(KIND, "old", OLD, beatsPerMinute=zero),
        summary(KIND, "new", DAY, beatsPerMinute=zero),
    )
    await history(sessions, OLD, DAY, None)


@pytest.mark.parametrize("batch", [1, 2])
@pytest.mark.parametrize("alternative", [False, True])
async def test_ordered_candidates_corrections_and_other_platform(
    sessions, monkeypatch, clock, batch, alternative
):
    monkeypatch.setattr(reader, "_SUMMARY_BATCH", batch)
    # Equal last_seen: reverse insertion order must still use the Google id tie-break.
    await store(
        sessions,
        KIND,
        summary(KIND, "z", OLD, beatsPerMinute=-1),
        summary(KIND, "a", OLD, beatsPerMinute=60),
        summary(KIND, "middle", MIDDLE, beatsPerMinute=60),
    )
    if alternative:
        await store(sessions, KIND, summary(KIND, "phone", OLD, source=PHONE, beatsPerMinute=80))
    for value, visible in (
        (None, False),
        (65, True),
        ("-1e-999999", False),
        ("NaN", False),
        (0, True),
    ):
        if value is not None:
            clock.advance(seconds=1)
            await store(sessions, KIND, summary(KIND, "a", OLD, beatsPerMinute=value))
        await history(sessions, OLD if visible or alternative else MIDDLE, MIDDLE, 60)
    # An unparseable *other* candidate leaves the prior decisive value intact.
    clock.advance(seconds=1)
    await store(sessions, KIND, summary(KIND, "text", OLD, beatsPerMinute="text"))
    await history(sessions, OLD, MIDDLE, 60)


@pytest.mark.parametrize("state", ["deleted", "raw_quarantine", "normalized_quarantine", "held"])
async def test_bounds_apply_the_daily_eligibility_policy(sessions, state):
    await store(
        sessions,
        KIND,
        summary(KIND, "old", OLD, beatsPerMinute=60),
        summary(KIND, "middle", MIDDLE, beatsPerMinute=65),
    )
    async with sessions.begin() as session:
        raws = (
            await session.scalars(select(RawHealthData).order_by(RawHealthData.source_start_time))
        ).all()
        old, new = raws
        if state == "deleted":
            old.source_deleted_at = T0
        elif state == "raw_quarantine":
            old.is_quarantined = True
        elif state == "normalized_quarantine":
            await session.execute(
                update(RestingHeartRate)
                .where(RestingHeartRate.google_data_point_id == old.google_data_point_id)
                .values(is_quarantined=True)
            )
        else:
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=old.id,
                    new_raw_id=new.id,
                    data_type=KIND,
                    old_google_data_point_id=old.google_data_point_id,
                    new_google_data_point_id=new.google_data_point_id,
                    reason="duplicate",
                    old_fingerprint="a" * 64,
                    new_fingerprint="a" * 64,
                    same_reading=True,
                    new_payload_hash=new.payload_hash,
                )
            )
    await history(sessions, MIDDLE, MIDDLE, 65)


@pytest.mark.parametrize("preferred,expected", [("FITBIT,GARMIN", 60), ("GARMIN,FITBIT", 50)])
async def test_union_with_garmin_keeps_bounds_and_source_precedence(
    sessions, monkeypatch, preferred, expected
):
    monkeypatch.setattr(get_settings(), "source_precedence_health", preferred)
    await store(
        sessions,
        KIND,
        summary(KIND, "old-invalid", OLD - timedelta(days=30), beatsPerMinute=-1),
        summary(KIND, "middle", MIDDLE, beatsPerMinute=60),
        summary(KIND, "new-invalid", DAY, beatsPerMinute="NaN"),
    )
    latest = DAY - timedelta(days=1)
    await store_page(
        sessions, "daily-summaries", [daily_summary(day) for day in (OLD, MIDDLE, latest)], T0
    )
    async with sessions.begin() as session:
        await normalize_window(session, OLD, DAY)
    await history(sessions, OLD, latest, expected)
    async with sessions() as session:
        assert await reader.archive_bounds(session, "heart", DAY, ZoneInfo("UTC")) == ArchiveBounds(
            MIDDLE, MIDDLE
        )
