"""Read the Garmin rows of the shared normalized tables (``provider = 'GARMIN'``).

The Google readers find a normalized row through its ``raw_health_data`` row and so never see
a Garmin row; these readers select by provider and so never see a Google row. What they return
is fed to the precedence resolver (app/services/identities/source_precedence.py) next to the Google
values; they never sum across providers. Read-only.
"""

from __future__ import annotations

from collections.abc import Collection
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
from app.services.interpretation.sleep_estimate import sleep_estimate
from app.services.readers import HistoryMetric, checked_dates, daily_window
from app.services.readers.dashboard_rows import DAILY_METRICS
from app.services.readers.reading import ArchiveBounds, WorkoutRecord
from app.services.shared.finite_math import finite_number

from .garmin_versions import current_row


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

    low, high = f"steps:{first.isoformat()}", f"steps:{last.isoformat()}"
    step_rows = (
        (
            await session.execute(
                select(Steps.source_record_id, Steps.count).where(
                    Steps.provider == provider,
                    current_row(Steps),
                    Steps.is_quarantined.is_(False),
                    Steps.source_record_id >= low,
                    Steps.source_record_id <= high,
                )
            )
        ).tuples()
        if "steps" in metrics
        else ()
    )
    for key, count in step_rows:
        put(_day_of(key), "steps", _float(count))
    for model, metric, column, kind in (
        (RestingHeartRate, "resting_bpm", RestingHeartRate.bpm, "resting-heart-rate"),
        (HeartRateVariability, "hrv_ms", HeartRateVariability.rmssd, "hrv"),
        (OxygenSaturation, "spo2", OxygenSaturation.spo2, "spo2"),
    ):
        if metric not in metrics:
            continue
        rows = await session.execute(
            select(model.source_record_id, column).where(
                model.provider == provider,
                current_row(model),
                model.is_quarantined.is_(False),
                model.source_record_id >= f"{kind}:{first.isoformat()}",
                model.source_record_id <= f"{kind}:{last.isoformat()}",
            )
        )
        for key, value in rows.tuples():
            put(_day_of(key), metric, _float(value))
    if "sleep_seconds" not in metrics:
        return days
    # Sleep by wake-up day: the nights that end inside the window's civil days.
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    stop = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
    nights = (
        await session.scalars(
            select(SleepSession).where(
                SleepSession.provider == provider,
                current_row(SleepSession),
                SleepSession.is_quarantined.is_(False),
                SleepSession.end_time >= start,
                SleepSession.end_time < stop,
            )
        )
    ).all()

    by_day: dict[date, list[SleepSession]] = {}
    for night in nights:
        by_day.setdefault(as_utc(night.end_time).astimezone(zone).date(), []).append(night)
    for day, sessions in by_day.items():
        put(day, "sleep_seconds", sleep_estimate([(row, None) for row in sessions]).seconds)
    return days


async def heart_rate_samples(
    session: AsyncSession, start: datetime, stop: datetime, provider: str = GARMIN
) -> list[tuple[datetime, float]]:
    """Current Garmin samples in ``[start, stop)``, including daily set and FIT eligibility."""
    rows = await session.execute(
        select(HeartRate.timestamp, HeartRate.bpm)
        .where(
            HeartRate.provider == provider,
            current_row(HeartRate),
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


async def workouts(
    session: AsyncSession, start: datetime, stop: datetime, provider: str = GARMIN
) -> list[WorkoutRecord]:
    """Garmin's exercise sessions starting in ``[start, stop)`` as workout rows (the shape of
    ``app.services.readers.current_workouts.current_workout``, source ``GARMIN``)."""
    rows = (
        await session.scalars(
            select(ExerciseSession)
            .where(
                ExerciseSession.provider == provider,
                current_row(ExerciseSession),
                ExerciseSession.is_quarantined.is_(False),
                ExerciseSession.start_time >= start,
                ExerciseSession.start_time < stop,
            )
            .order_by(ExerciseSession.start_time)
        )
    ).all()
    found: list[WorkoutRecord] = []
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
                "source_code": provider,
                "provider": provider,
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
    if metric == "sleep":
        stop = daily_window(today, 1, zone).stop
        earliest, latest = (
            await session.execute(
                select(func.min(SleepSession.end_time), func.max(SleepSession.end_time)).where(
                    SleepSession.provider == GARMIN,
                    current_row(SleepSession),
                    SleepSession.is_quarantined.is_(False),
                    SleepSession.end_time > SleepSession.start_time,
                    SleepSession.end_time < stop,
                )
            )
        ).one()
        with checked_dates():
            return ArchiveBounds(
                as_utc(earliest).astimezone(zone).date() if earliest else None,
                as_utc(latest).astimezone(zone).date() if latest else None,
            )
    # Daily rows belong to the calendar date in their key, regardless of UTC offset.
    model, kind = (
        (Steps, "steps") if metric == "steps" else (RestingHeartRate, "resting-heart-rate")
    )
    earliest, latest = (
        await session.execute(
            select(func.min(model.source_record_id), func.max(model.source_record_id)).where(
                model.provider == GARMIN,
                current_row(model),
                model.is_quarantined.is_(False),
                model.source_record_id >= f"{kind}:{date.min.isoformat()}",
                model.source_record_id <= f"{kind}:{today.isoformat()}",
            )
        )
    ).one()
    return ArchiveBounds(_day_of(earliest), _day_of(latest))
