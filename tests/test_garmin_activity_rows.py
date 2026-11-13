"""Activity time bounds, numeric preparation and archive-preserving overflow handling."""

from datetime import UTC, datetime, timedelta

import pytest
from garmin_records import activity, store_page
from sqlalchemy import select
from test_garmin_bounded import FIRST, NOW, CountingSessions
from test_garmin_bounded import sessions as sessions

from app.models.garmin import GarminApiResponse, GarminRawRecord, GarminRecordObservation
from app.models.health import ExerciseSession
from app.services.providers import garmin_bounded
from app.services.providers.garmin_normalize import build_rows


async def test_overflowing_activities_skip_without_losing_pages_or_archive(sessions, monkeypatch):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 2)
    bodies = [activity(key, FIRST) for key in range(1, 8)]
    bodies[0]["duration"] = "1e30"
    bodies[2]["duration"] = "1e1000"
    bodies[4].update(startTimeGMT="9999-12-31T23:59:50", duration=60)
    for offset in range(0, len(bodies), 2):
        await store_page(sessions, "activities", bodies[offset : offset + 2], NOW)

    async def snapshot(models):
        async with sessions() as session:
            return [
                (await session.execute(select(model.__table__).order_by(model.id))).all()
                for model in models
            ]

    archive_models = (GarminApiResponse, GarminRawRecord, GarminRecordObservation)
    before = await snapshot(archive_models)
    assert [len(rows) for rows in before] == [4, 7, 7]
    counting = CountingSessions(sessions)
    stats = await garmin_bounded.normalize_bounded(counting, FIRST, FIRST, device="Garmin")
    assert counting.begun == 5  # one daily chunk and four activity pages
    assert (stats.inserted, stats.updated, stats.skipped) == (4, 0, 3)
    async with sessions() as session:
        rows = (await session.scalars(select(ExerciseSession))).all()
        assert {row.source_record_id for row in rows} == {f"activities:{i}" for i in (2, 4, 6, 7)}
        assert all(
            row.end_time - row.start_time == timedelta(seconds=activity(1, FIRST)["duration"])
            for row in rows
        )
    normalized = await snapshot((ExerciseSession,))
    assert await snapshot(archive_models) == before
    again = await garmin_bounded.normalize_bounded(sessions, FIRST, FIRST, device="Garmin")
    assert (again.inserted, again.updated, again.unchanged, again.skipped) == (0, 0, 4, 3)
    assert await snapshot((ExerciseSession,)) == normalized
    assert await snapshot(archive_models) == before


@pytest.mark.parametrize(
    "duration, explicit_end",
    [("1e30", True), ("1e1000", True), (None, True), (0, True), (-1, True), (10**10, False)],
)
def test_activity_end_preserves_explicit_bounds_and_optional_nulls(duration, explicit_end):
    body = {**activity(1, FIRST), "duration": duration, "calories": "1e1000", "distance": "1e30"}
    start = datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
    end = start + timedelta(seconds=3600 if explicit_end else duration)
    if explicit_end:
        body["endTimeGMT"] = end.isoformat()
    rows, skipped = build_rows({"activities": {"1": body}}, FIRST, FIRST, "Garmin")
    assert skipped == 0
    (row,) = rows[ExerciseSession]
    assert row["start_time"] == start and row["end_time"] == end
    assert row["duration_seconds"] == (duration if isinstance(duration, int) else None)
    assert row["calories_kcal"] is row["distance_meters"] is None


@pytest.mark.parametrize("end", [None, "invalid", "2026-01-01T17:00:00Z", "2026-01-01T16:00:00Z"])
def test_activity_duration_fallback_keeps_fractional_seconds(end):
    body = {**activity(1, FIRST), "duration": 2410.25, "endTimeGMT": end}
    rows, skipped = build_rows({"activities": {"1": body}}, FIRST, FIRST, "Garmin")
    assert skipped == 0
    (row,) = rows[ExerciseSession]
    assert row["end_time"] - row["start_time"] == timedelta(seconds=2410.25)
    assert row["duration_seconds"] == 2410.25
