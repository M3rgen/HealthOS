"""Garmin observations, completed projections and clock publication invalidate history."""

from datetime import UTC, datetime, timedelta

import garmin_records
import pytest
from garmin_projection_archive import payload as garmin_payload
from history_cache_archive import TODAY
from sqlalchemy import select, update
from test_history_bounds_cache import MIDDLE, checked

from app.models.garmin import GarminDailyProjection, GarminDayTime
from app.models.health import Steps
from app.services.providers.garmin_normalize import normalize_window


def sleep_body(day):
    return garmin_payload("sleep", ("sleepTimeSeconds",), day, 42)


@pytest.mark.parametrize(
    "metric,kind,field",
    [
        ("steps", "steps", "totalSteps"),
        ("heart", "heart-rate", "restingHeartRate"),
        ("sleep", "sleep", "sleepTimeSeconds"),
    ],
)
async def test_observed_version_publication_and_return(sessions, monkeypatch, metric, kind, field):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    old = TODAY - timedelta(days=95)

    def body(day, invalid=False):
        factory = {
            "steps": garmin_records.daily_steps,
            "heart": garmin_records.heart_rate,
            "sleep": sleep_body,
        }[metric]
        result = factory(day)
        if invalid:
            result[field] = -1
        return result

    for day in (old, MIDDLE):
        await garmin_records.store_page(sessions, kind, [body(day)], now)
    async with sessions.begin() as session:
        await normalize_window(session, old, MIDDLE)
    await checked(sessions, monkeypatch, metric, old, MIDDLE)
    for index, invalid in enumerate((True, False), 1):
        await garmin_records.store_page(
            sessions, kind, [body(old, invalid)], now + timedelta(minutes=index)
        )
        # A raw version/observation alone cannot change a published measurement.
        await checked(sessions, monkeypatch, metric, old if invalid else MIDDLE, MIDDLE)
        async with sessions.begin() as session:
            await normalize_window(session, old, old)
        await checked(sessions, monkeypatch, metric, MIDDLE if invalid else old, MIDDLE)


@pytest.mark.parametrize("publication", ["projection", "clock", "normalized_quarantine"])
async def test_individual_publication_tables(sessions, monkeypatch, publication):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    metric = "sleep" if publication == "clock" else "steps"
    kind = metric
    factory = sleep_body if metric == "sleep" else garmin_records.daily_steps
    await garmin_records.store_page(sessions, kind, [factory(MIDDLE)], now)
    async with sessions.begin() as session:
        await normalize_window(session, MIDDLE, MIDDLE)
    await checked(sessions, monkeypatch, metric, MIDDLE, MIDDLE)
    async with sessions.begin() as session:
        if publication == "projection":
            previous = (
                await session.scalars(
                    select(GarminDailyProjection).where(
                        GarminDailyProjection.source_record_id == f"steps:{MIDDLE}"
                    )
                )
            ).one()
            session.add(
                GarminDailyProjection(
                    source_record_id=previous.source_record_id,
                    raw_record_ids=previous.raw_record_ids,
                    normalized_key=None,
                )
            )
        elif publication == "clock":
            session.add(
                GarminDayTime(day=str(MIDDLE), day_ordinal=MIDDLE.toordinal(), raw_record_id=None)
            )
        else:
            await session.execute(update(Steps).values(is_quarantined=True))
    expected = MIDDLE if publication == "clock" else None
    await checked(sessions, monkeypatch, metric, expected, expected)
