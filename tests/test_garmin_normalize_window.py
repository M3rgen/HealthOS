"""Keep UTC activity starts adjacent to the requested civil-day window."""

from datetime import UTC, date, datetime

import garmin_records
from sqlalchemy import select

from app.core.providers import garmin_record_key
from app.models.health import ExerciseSession
from app.services.providers.garmin_normalize import normalize_window


async def test_activity_window_keeps_one_day_margin_but_excludes_two_days_before(sessions):
    starts = (
        "2026-08-09 22:30:00",
        "2026-08-10 12:00:00",
        "2026-08-12 01:00:00",
        "2026-08-08 12:00:00",
    )
    bodies = [
        {**garmin_records.activity(index, date.fromisoformat(start[:10])), "startTimeGMT": start}
        for index, start in enumerate(starts, 1)
    ]
    await garmin_records.store_page(
        sessions, "activities", bodies, datetime(2026, 8, 13, tzinfo=UTC)
    )

    async with sessions.begin() as session:
        stats = await normalize_window(session, date(2026, 8, 10), date(2026, 8, 11))

    async with sessions() as session:
        rows = (await session.scalars(select(ExerciseSession))).all()
    assert stats.inserted == 3
    assert {row.google_data_point_id for row in rows} == {
        garmin_record_key("activities", str(index), "v2") for index in (1, 2, 3)
    }
    assert {row.start_time.replace(tzinfo=UTC) for row in rows} == {
        datetime.fromisoformat(start).replace(tzinfo=UTC) for start in starts[:3]
    }
