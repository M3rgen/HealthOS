"""Read the Garmin rows of the shared normalized tables (``provider = 'GARMIN'``).

The Google readers find a normalized row through its ``raw_health_data`` row and so never see
a Garmin row; these readers select by provider and so never see a Google row. What they return
is fed to the precedence resolver (app/services/identities/source_precedence.py) next to the Google
values; they never sum across providers. Read-only.
"""

from __future__ import annotations

from collections.abc import Collection
from contextlib import aclosing
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
    SleepSession,
    Steps,
)
from app.services.readers import HistoryMetric, checked_dates, daily_window
from app.services.readers.dashboard_rows import DAILY_METRICS
from app.services.readers.reading import ArchiveBounds, WorkoutRecord
from app.services.shared.finite_math import finite_number

from .garmin_daily_values import SCALARS, scalar_values
from .garmin_heart_values import heart_values
from .garmin_sleep_values import iter_sleep_days
from .garmin_steps import step_values
from .garmin_versions import current_row
from .garmin_workout_values import workout_values


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
    session: AsyncSession,
    first: date,
    last: date,
    zone: ZoneInfo,
    *,
    metrics: Collection[str] = DAILY_METRICS,
    provider: str = GARMIN,
) -> dict[date, dict[str, float]]:
    """day -> {metric: value} from Garmin's daily rows: ``steps`` (the civil day's total),
    ``resting_bpm``, ``hrv_ms`` and ``spo2`` by the record's calendar day, ``sleep_seconds``
    by the night's wake-up day in ``zone`` (the dashboard's rule), asleep time estimated as
    the dashboard does from the stage columns (``sleep_estimate``). Only ``metrics`` are read.
    ``provider`` reads another provider's rows of the same shape (the sample data, ``DEMO``)."""
    days: dict[date, dict[str, float]] = {}

    def put(day: date | None, metric: str, value: float | None) -> None:
        if day is not None and value is not None and first <= day <= last:
            days.setdefault(day, {})[metric] = value

    if "steps" in metrics:
        async for key, value in step_values(session, first, last, provider):
            put(_day_of(key), "steps", value)
    for metric in SCALARS:
        if metric in metrics:
            async for key, value in scalar_values(session, metric, first, last, provider):
                put(_day_of(key), metric, value)
    if "sleep_seconds" not in metrics:
        return days
    # Sleep by wake-up day: the nights that end inside the window's civil days.
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    stop = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
    async with aclosing(iter_sleep_days(session, start, stop, zone, provider)) as values:
        async for day, seconds in values:
            put(day, "sleep_seconds", seconds)
    return days


async def heart_rate_samples(
    session: AsyncSession, start: datetime, stop: datetime, provider: str = GARMIN
) -> list[tuple[datetime, float]]:
    """Current Garmin samples in ``[start, stop)``, including daily set and FIT eligibility."""
    return await heart_values(session, start, stop, provider)


async def workouts(
    session: AsyncSession, start: datetime, stop: datetime, provider: str = GARMIN
) -> list[WorkoutRecord]:
    """Garmin's exercise sessions starting in ``[start, stop)`` as workout rows (the shape of
    ``app.services.readers.current_workouts.current_workout``, source ``GARMIN``)."""
    found: list[WorkoutRecord] = []
    async for row, calories, distance, duration in workout_values(session, start, stop, provider):
        begin, end = as_utc(row.start_time), as_utc(row.end_time)
        if end <= begin:
            continue
        found.append(
            {
                "id": str(row.id),
                "exercise_type": row.exercise_type,
                "start_time": begin,
                "end_time": end,
                "duration_seconds": duration,
                "calories_kcal": calories,
                "distance_meters": distance,
                "source_code": provider,
                "provider": provider,
            }
        )
    return found


async def activity_windows(
    session: AsyncSession, start: datetime, stop: datetime, provider: str = GARMIN
) -> list[tuple[datetime, datetime]]:
    """Current positive exercise intervals overlapping ``[start, stop)`` for heart policy.

    Sessions can span any number of days: select by intersection, independently of the
    start-based sports list. Read only endpoints and apply the same provider, version,
    quarantine and FIT eligibility rules as the workout reader.
    """
    rows = await session.execute(
        select(ExerciseSession.start_time, ExerciseSession.end_time)
        .where(
            ExerciseSession.provider == provider,
            current_row(ExerciseSession),
            ExerciseSession.is_quarantined.is_(False),
            ExerciseSession.start_time < stop,
            ExerciseSession.end_time > start,
            ExerciseSession.end_time > ExerciseSession.start_time,
        )
        .order_by(ExerciseSession.start_time)
    )
    return [(as_utc(begin), as_utc(end)) for begin, end in rows.tuples()]


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
                model.provider == GARMIN, current_row(model), model.is_quarantined.is_(False)
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


async def archive_bounds(
    session: AsyncSession, metric: HistoryMetric, today: date, zone: ZoneInfo
) -> ArchiveBounds:
    """Aggregate only the selected metric's current, non-quarantined archive dates."""
    if metric != "sleep":
        first = last = None
        values = (
            step_values(session, date.min, today)
            if metric == "steps"
            else scalar_values(session, "resting_bpm", date.min, today)
        )
        async for key, _ in values:
            day = _day_of(key)
            if day is not None:
                first = min(first, day) if first is not None else day
                last = max(last, day) if last is not None else day
        return ArchiveBounds(first, last)
    stop = daily_window(today, 1, zone).stop
    with checked_dates():
        first = last = None
        async with aclosing(
            iter_sleep_days(session, datetime.min.replace(tzinfo=UTC), stop, zone)
        ) as values:
            async for day, _ in values:
                first = min(first, day) if first is not None else day
                last = max(last, day) if last is not None else day
        return ArchiveBounds(first, last)
