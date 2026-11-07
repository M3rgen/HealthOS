"""Read the Garmin rows of the shared normalized tables (``provider = 'GARMIN'``).

The Google readers find a normalized row through its ``raw_health_data`` row and so never see
a Garmin row; these readers select by provider and so never see a Google row. What they return
is fed to the precedence resolver (app/services/source_precedence.py) next to the Google
values; they never sum across providers. Read-only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import GARMIN
from app.core.timeutil import as_utc
from app.models.garmin import GarminPullState, GarminRawRecord
from app.models.health import (
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    SleepSession,
    Steps,
)
from app.services.finite_math import finite_number


def _float(value: Decimal | None) -> float | None:
    return finite_number(value) if value is not None else None


def _day_of(row_key: str | None) -> date | None:
    """The calendar day of a day-keyed Garmin row from its ``source_record_id``
    (``<kind>:YYYY-MM-DD``)."""
    if not row_key:
        return None
    try:
        return date.fromisoformat(row_key.split(":", 1)[1][:10])
    except (IndexError, ValueError):
        return None


async def daily_values(
    session: AsyncSession, first: date, last: date, zone: ZoneInfo
) -> dict[date, dict[str, float]]:
    """day -> {metric: value} from Garmin's daily rows: ``steps`` (the civil day's total),
    ``resting_bpm``, ``hrv_ms`` and ``spo2`` by the record's calendar day, ``sleep_seconds``
    by the night's wake-up day in ``zone`` (the dashboard's rule), asleep time estimated as
    the dashboard does from the stage columns (``sleep_estimate``)."""
    days: dict[date, dict[str, float]] = {}

    def put(day: date | None, metric: str, value: float | None) -> None:
        if day is not None and value is not None and first <= day <= last:
            days.setdefault(day, {})[metric] = value

    low, high = f"steps:{first.isoformat()}", f"steps:{last.isoformat()}"
    for key, count in (
        await session.execute(
            select(Steps.source_record_id, Steps.count).where(
                Steps.provider == GARMIN,
                Steps.is_quarantined.is_(False),
                Steps.source_record_id >= low,
                Steps.source_record_id <= high,
            )
        )
    ).tuples():
        put(_day_of(key), "steps", _float(count))
    for model, metric, column, kind in (
        (RestingHeartRate, "resting_bpm", RestingHeartRate.bpm, "resting-heart-rate"),
        (HeartRateVariability, "hrv_ms", HeartRateVariability.rmssd, "hrv"),
        (OxygenSaturation, "spo2", OxygenSaturation.spo2, "spo2"),
    ):
        rows = await session.execute(
            select(model.source_record_id, column).where(
                model.provider == GARMIN,
                model.is_quarantined.is_(False),
                model.source_record_id >= f"{kind}:{first.isoformat()}",
                model.source_record_id <= f"{kind}:{last.isoformat()}",
            )
        )
        for key, value in rows.tuples():
            put(_day_of(key), metric, _float(value))
    # Sleep by wake-up day: the nights that end inside the window's civil days.
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    stop = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
    nights = (
        await session.scalars(
            select(SleepSession).where(
                SleepSession.provider == GARMIN,
                SleepSession.is_quarantined.is_(False),
                SleepSession.end_time >= start,
                SleepSession.end_time < stop,
            )
        )
    ).all()
    from app.services.dashboard import sleep_estimate  # the dashboard imports this module

    by_day: dict[date, list[SleepSession]] = {}
    for night in nights:
        by_day.setdefault(as_utc(night.end_time).astimezone(zone).date(), []).append(night)
    for day, sessions in by_day.items():
        put(day, "sleep_seconds", sleep_estimate([(row, None) for row in sessions]).seconds)
    return days


async def heart_rate_samples(
    session: AsyncSession, start: datetime, stop: datetime
) -> list[tuple[datetime, float]]:
    """Garmin's heart-rate samples in ``[start, stop)``, time-ordered."""
    rows = await session.execute(
        select(HeartRate.timestamp, HeartRate.bpm)
        .where(
            HeartRate.provider == GARMIN,
            HeartRate.is_quarantined.is_(False),
            HeartRate.timestamp >= start,
            HeartRate.timestamp < stop,
        )
        .order_by(HeartRate.timestamp)
    )
    found = []
    for moment, bpm in rows.tuples():
        value = _float(bpm)
        if value is not None:
            found.append((as_utc(moment), value))
    return found


async def workouts(session: AsyncSession, start: datetime, stop: datetime) -> list[dict[str, Any]]:
    """Garmin's exercise sessions starting in ``[start, stop)`` as workout rows (the shape of
    ``app.services.current_workouts.current_workout``, source ``GARMIN``)."""
    rows = (
        await session.scalars(
            select(ExerciseSession)
            .where(
                ExerciseSession.provider == GARMIN,
                ExerciseSession.is_quarantined.is_(False),
                ExerciseSession.start_time >= start,
                ExerciseSession.start_time < stop,
            )
            .order_by(ExerciseSession.start_time)
        )
    ).all()
    found = []
    for row in rows:
        begin, end = as_utc(row.start_time), as_utc(row.end_time)
        if end <= begin:
            continue
        found.append(
            {
                "id": str(row.id),
                "exercise_type": row.exercise_type,
                "start_time": begin,
                "end_time": end,
                "duration_seconds": (
                    _float(row.duration_seconds)
                    if row.duration_seconds is not None
                    else (end - begin).total_seconds()
                ),
                "calories_kcal": _float(row.calories_kcal),
                "distance_meters": _float(row.distance_meters),
                "source_code": GARMIN,
                "provider": GARMIN,
            }
        )
    return found


async def archive_summary(session: AsyncSession) -> dict[str, Any] | None:
    """What Cihazlarım shows for the Garmin source: the kinds with records, the first archive
    time, the last successful pull and the newest normalized data time; None when the archive
    has no Garmin record at all."""
    kinds = (
        (
            await session.execute(
                select(GarminRawRecord.kind, func.min(GarminRawRecord.first_seen_at)).group_by(
                    GarminRawRecord.kind
                )
            )
        )
        .tuples()
        .all()
    )
    if not kinds:
        return None
    pulls = (await session.scalars(select(GarminPullState.last_success_at))).all()
    newest = [
        await session.scalar(
            select(func.max(column)).where(
                model.provider == GARMIN, model.is_quarantined.is_(False)
            )
        )
        for model, column in (
            (HeartRate, HeartRate.timestamp),
            (Steps, Steps.end_time),
            (SleepSession, SleepSession.end_time),
            (ExerciseSession, ExerciseSession.end_time),
        )
    ]
    dated = [as_utc(value) for value in newest if value is not None]
    successes = [as_utc(value) for value in pulls if value is not None]
    return {
        "data_types": sorted(kind for kind, _ in kinds),
        "first_seen": min(as_utc(first) for _, first in kinds),
        "last_pull": max(successes) if successes else None,
        "last_data": max(dated) if dated else None,
    }
