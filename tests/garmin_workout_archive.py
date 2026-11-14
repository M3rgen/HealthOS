"""Synthetic daily workouts through production writers, including pre-result archives."""

from datetime import UTC, datetime, timedelta

from garmin_records import activity
from sqlalchemy import select
from test_garmin_activity_legacy_ids import legacy_page, legacy_row

from app.models.garmin import GarminRawRecord
from app.services.providers.garmin_activity_state import write_activity_results
from app.services.providers.garmin_normalize import build_rows, write_rows

START = datetime(2000, 1, 1, tzinfo=UTC)
STOP = datetime(2020, 1, 1, tzinfo=UTC)
SEEN = datetime(2026, 1, 1, tzinfo=UTC)


async def grow_archive(sessions, monkeypatch, first, last):
    """One workout/day; one quarter published, one tenth uses old full-id rows."""
    for offset in range(first, last, 250):
        records = []
        for i in range(offset, offset + 250):
            record = activity(i, START.date() + timedelta(days=i))
            if i % 10 == 9:
                record["activityId"] = f"{i:064d}legacy"
            records.append(record)
        await legacy_page(sessions, monkeypatch, records, SEEN)
        outcomes = {}
        rows, _ = build_rows(
            {
                "activities": {
                    str(r["activityId"]): r for r in records if len(str(r["activityId"])) <= 64
                }
            },
            START.date(),
            STOP.date(),
            "Garmin",
            activity_results=outcomes,
        )
        async with sessions.begin() as session:
            for model, values in rows.items():
                await write_rows(session, model, values)
            session.add_all(legacy_row(r, "v1") for r in records if len(str(r["activityId"])) > 64)
            published = {key: value for key, value in outcomes.items() if int(key) % 4 == 0}
            ids = (
                await session.execute(
                    select(GarminRawRecord.key, GarminRawRecord.id).where(
                        GarminRawRecord.key.in_(published)
                    )
                )
            ).all()
            await write_activity_results(
                session, {("activities", key): value for key, value in ids}, published
            )
