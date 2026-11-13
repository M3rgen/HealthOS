"""Read the Apple rows of the shared normalized tables (``provider = 'APPLE'``).

Like the Garmin readers (app/services/providers/garmin_readers.py) these select by provider, so they
never see a Google row, and the Google readers (which join a row to its ``raw_health_data``
row) never see an Apple one. What they return is fed to the precedence resolver
(app/services/identities/source_precedence.py) under the source code ``HEALTH_KIT`` with provider
``APPLE``; nothing is summed across providers. Read-only.

No double counting. Apple Health keeps every source's samples: a Watch and an iPhone both
count the same steps, a Watch and a sleep app both record the same night, and the owner may
send the same Health store twice (Health Auto Export, channel ``hae``, and an ``export.zip``
upload, channel ``export``). The source of a row is its ``device`` column (the Health source,
app/services/apple_normalize.py), its channel the first field of ``source_record_id``. Rules:

* **steps**: per channel, every sample is split proportionally into the local hours it
  covers (a sample without duration falls in the hour of its start); in each hour the single
  source with the most steps is taken (sources are never added); the day is the sum of its
  hours. Both channels describe the same Health store, so the day's value is the larger of
  the two channel totals, never their sum.
* **sleep_seconds**: per channel and source, the union of the asleep segments (Core, Deep,
  REM, Asleep/Unspecified; never In Bed or Awake) is grouped into nights (segments less than
  three hours apart), and each night is assigned to the local day it ends (the dashboard's
  wake-up day rule); a Health Auto Export summarized night counts with its ``totalSleep``.
  Per channel and day the source with the most asleep time is taken; then the larger channel.
* **resting_bpm**: the newest record of the local day (any channel).
* **hrv_ms**: the mean of the local day's SDNN samples (``metrics.sdnn_ms``), one sample per
  timestamp across channels. Apple's HRV is SDNN, Google's/Fitbit's RMSSD: they are shown as
  alternatives of one another, never combined.
* **spo2**: the mean percent of the local day's samples, one per timestamp.
* heart-rate samples: one per timestamp across channels.
* workouts: a workout of one channel that overlaps a workout of the other channel by at least
  half of the shorter one is the same workout; the export's copy is kept.

Every query is bounded to the window on an indexed time column.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apple.records import EXPORT
from app.core.providers import APPLE
from app.core.timeutil import as_utc
from app.models.apple import AppleSource
from app.models.health import (
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    SleepSession,
    Steps,
)
from app.services import shared
from app.services.readers import dashboard_rows, dashboard_window, reading

from .apple_normalize import ASLEEP_STAGES, NIGHT, channel_of, sleep_stage

HEALTH_KIT = "HEALTH_KIT"
# Asleep segments of one source less than this far apart belong to one night.
NIGHT_GAP = timedelta(hours=3)
# The longest step sample or sleep night a window query reaches back for.
STEP_REACH = timedelta(days=2)
SLEEP_REACH = timedelta(hours=36)
# Two workouts are the same one when they overlap by at least this share of the shorter.
SAME_WORKOUT = 0.5
# Stores one day value: (local day, metric, value or None).
type _Put = Callable[[date, str, float | None], None]


def _float(value: Decimal | None) -> float | None:
    return shared.finite_math.finite_number(value) if value is not None else None


def _before(moment: datetime, span: timedelta) -> datetime:
    """``moment - span``, held at the first representable instant (a year-1 window)."""
    try:
        return moment - span
    except OverflowError:
        return datetime.min.replace(tzinfo=UTC)


def _bounds(first: date, last: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    stop = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
    return start, stop


def overlap_share(
    left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime
) -> float:
    """The overlap of two intervals as a share of the shorter one (0 when either is empty)."""
    shorter = min(
        (left_end - left_start).total_seconds(), (right_end - right_start).total_seconds()
    )
    if shorter <= 0:
        return 0.0
    overlap = (min(left_end, right_end) - max(left_start, right_start)).total_seconds()
    return max(0.0, overlap) / shorter


def _hours(
    left: datetime, right: datetime, start: datetime, stop: datetime, zone: ZoneInfo
) -> list[tuple[datetime, float]]:
    """(UTC start of the local hour, share of the sample) for each local hour the sample
    ``[left, right)`` covers inside ``[start, stop)``."""
    duration = (right - left).total_seconds()
    if duration <= 0:
        if start <= left < stop:
            local = left.astimezone(zone)
            hour = left - timedelta(
                minutes=local.minute, seconds=local.second, microseconds=local.microsecond
            )
            return [(hour, 1.0)]
        return []
    found = []
    cursor = max(left, start)
    while cursor < min(right, stop):
        local = cursor.astimezone(zone)
        hour = cursor - timedelta(
            minutes=local.minute, seconds=local.second, microseconds=local.microsecond
        )
        edge = min(hour + timedelta(hours=1), right, stop)
        found.append((hour, (edge - cursor).total_seconds() / duration))
        cursor = edge
    return found


async def _steps(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo
) -> dict[date, float]:
    rows = await session.execute(
        select(
            Steps.source_record_id, Steps.device, Steps.start_time, Steps.end_time, Steps.count
        ).where(
            Steps.provider == APPLE,
            Steps.is_quarantined.is_(False),
            Steps.start_time >= _before(start, STEP_REACH),
            Steps.start_time < stop,
            Steps.end_time >= start,
        )
    )
    # (channel, hour) -> source -> steps
    hours: dict[tuple[str | None, datetime], dict[str | None, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for key, device, left, right, count in rows.tuples():
        value = _float(count)
        if value is None:
            continue
        for hour, share in _hours(as_utc(left), as_utc(right), start, stop, zone):
            hours[(channel_of(key), hour)][device] += value * share
    totals: dict[tuple[str | None, date], float] = defaultdict(float)
    for (channel, hour), sources in hours.items():
        totals[(channel, hour.astimezone(zone).date())] += max(sources.values())
    days: dict[date, float] = {}
    for (_, day), value in totals.items():
        days[day] = max(days.get(day, 0.0), value)
    return {day: float(round(value)) for day, value in days.items()}


def _union(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


async def _sleep(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo
) -> dict[date, float]:
    rows = await session.execute(
        select(
            SleepSession.source_record_id,
            SleepSession.device,
            SleepSession.start_time,
            SleepSession.end_time,
            SleepSession.duration_seconds,
        ).where(
            SleepSession.provider == APPLE,
            SleepSession.is_quarantined.is_(False),
            SleepSession.start_time >= _before(start, SLEEP_REACH),
            SleepSession.start_time < stop,
        )
    )
    # (channel, day) -> source -> asleep seconds, from segments and from summarized nights
    # apart (one night sent both ways is never added to itself).
    found: dict[tuple[str | None, date], dict[str | None, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    summarized: dict[tuple[str | None, date], dict[str | None, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    segments: dict[tuple[str | None, str | None], list[tuple[datetime, datetime]]] = defaultdict(
        list
    )
    for key, device, left, right, duration in rows.tuples():
        left, right = as_utc(left), as_utc(right)
        if right <= left:
            continue
        stage, channel = sleep_stage(key), channel_of(key)
        if stage == NIGHT:
            if start <= right < stop:
                seconds = _float(duration)
                if seconds is None:
                    seconds = (right - left).total_seconds()
                summarized[(channel, right.astimezone(zone).date())][device] += seconds
        elif stage in ASLEEP_STAGES:
            segments[(channel, device)].append((left, right))
    for (channel, device), intervals in segments.items():
        nights: list[list[tuple[datetime, datetime]]] = []
        for interval in _union(intervals):
            if nights and interval[0] - nights[-1][-1][1] < NIGHT_GAP:
                nights[-1].append(interval)
            else:
                nights.append([interval])
        for night in nights:
            end = night[-1][1]
            if not start <= end < stop:
                continue
            asleep = sum((right - left).total_seconds() for left, right in night)
            found[(channel, end.astimezone(zone).date())][device] += asleep
    days: dict[date, float] = {}
    for table in (found, summarized):
        for (_, day), sources in table.items():
            days[day] = max(days.get(day, 0.0), max(sources.values()))
    return days


async def daily_values(
    session: AsyncSession,
    first: date,
    last: date,
    zone: ZoneInfo,
    *,
    metrics: Collection[str] = dashboard_rows.DAILY_METRICS,
) -> dict[date, dict[str, float]]:
    """day -> {metric: value} for ``steps``, ``sleep_seconds``, ``resting_bpm``, ``hrv_ms``
    and ``spo2`` over the local days ``first``..``last`` (rules: the module docstring). Only
    ``metrics`` are read."""
    start, stop = _bounds(first, last, zone)
    days: dict[date, dict[str, float]] = {}

    def put(day: date, metric: str, value: float | None) -> None:
        if value is not None and first <= day <= last:
            days.setdefault(day, {})[metric] = value

    if "steps" in metrics:
        for day, value in (await _steps(session, start, stop, zone)).items():
            put(day, "steps", value)
    if "sleep_seconds" in metrics:
        for day, value in (await _sleep(session, start, stop, zone)).items():
            put(day, "sleep_seconds", value)
    if "resting_bpm" in metrics:
        await _resting(session, start, stop, zone, put)
    if "hrv_ms" in metrics:
        await _hrv(session, start, stop, zone, put)
    if "spo2" in metrics:
        await _spo2(session, start, stop, zone, put)
    return days


async def _resting(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo, put: _Put
) -> None:
    resting = await session.execute(
        select(RestingHeartRate.timestamp, RestingHeartRate.bpm)
        .where(
            RestingHeartRate.provider == APPLE,
            RestingHeartRate.is_quarantined.is_(False),
            RestingHeartRate.timestamp >= start,
            RestingHeartRate.timestamp < stop,
        )
        .order_by(RestingHeartRate.timestamp)
    )
    for moment, bpm in resting.tuples():  # ascending: the newest of the day is put last
        put(as_utc(moment).astimezone(zone).date(), "resting_bpm", _float(bpm))


async def _hrv(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo, put: _Put
) -> None:
    hrv = await session.execute(
        select(HeartRateVariability.timestamp, HeartRateVariability.metrics).where(
            HeartRateVariability.provider == APPLE,
            HeartRateVariability.is_quarantined.is_(False),
            HeartRateVariability.timestamp >= start,
            HeartRateVariability.timestamp < stop,
        )
    )
    sdnn: dict[datetime, float] = {}
    for moment, metrics in hrv.tuples():
        value = (
            shared.finite_math.finite_number(metrics.get("sdnn_ms"))
            if isinstance(metrics, dict)
            else None
        )
        if value is not None:
            sdnn.setdefault(as_utc(moment), value)
    for day, value in _daily_means(sdnn, zone).items():
        put(day, "hrv_ms", value)


async def _spo2(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo, put: _Put
) -> None:
    spo2 = await session.execute(
        select(OxygenSaturation.timestamp, OxygenSaturation.spo2).where(
            OxygenSaturation.provider == APPLE,
            OxygenSaturation.is_quarantined.is_(False),
            OxygenSaturation.timestamp >= start,
            OxygenSaturation.timestamp < stop,
        )
    )
    saturation: dict[datetime, float] = {}
    for moment, value in spo2.tuples():
        number = _float(value)
        if number is not None:
            saturation.setdefault(as_utc(moment), number)
    for day, value in _daily_means(saturation, zone).items():
        put(day, "spo2", value)


def _daily_means(samples: dict[datetime, float], zone: ZoneInfo) -> dict[date, float]:
    by_day: dict[date, list[float]] = defaultdict(list)
    for moment, value in samples.items():
        by_day[moment.astimezone(zone).date()].append(value)
    return {day: round(sum(values) / len(values), 3) for day, values in by_day.items()}


async def heart_rate_samples(
    session: AsyncSession, start: datetime, stop: datetime
) -> list[tuple[datetime, float]]:
    """Apple's heart-rate samples in ``[start, stop)``, one per timestamp, time-ordered."""
    rows = await session.execute(
        select(HeartRate.timestamp, HeartRate.bpm)
        .where(
            HeartRate.provider == APPLE,
            HeartRate.is_quarantined.is_(False),
            HeartRate.timestamp >= start,
            HeartRate.timestamp < stop,
        )
        .order_by(HeartRate.timestamp)
    )
    found: dict[datetime, float] = {}
    for moment, bpm in rows.tuples():
        value = _float(bpm)
        if value is not None:
            found.setdefault(as_utc(moment), value)
    return sorted(found.items())


async def workouts(
    session: AsyncSession, start: datetime, stop: datetime
) -> list[reading.WorkoutRecord]:
    """Apple's workouts starting in ``[start, stop)`` as workout rows (the shape of
    ``garmin_readers.workouts``, source ``HEALTH_KIT``, provider ``APPLE``); a workout both
    channels carry is listed once (the export's copy)."""
    rows = (
        await session.scalars(
            select(ExerciseSession)
            .where(
                ExerciseSession.provider == APPLE,
                ExerciseSession.is_quarantined.is_(False),
                ExerciseSession.start_time >= start,
                ExerciseSession.start_time < stop,
            )
            .order_by(ExerciseSession.start_time)
        )
    ).all()
    candidates = []
    for row in rows:
        begin, end = as_utc(row.start_time), as_utc(row.end_time)
        if end > begin:
            candidates.append((channel_of(row.source_record_id), begin, end, row))
    # The export first: a Health Auto Export copy of a kept export workout is left out.
    candidates.sort(key=lambda item: (item[0] != EXPORT, item[1], str(item[3].id)))
    kept: list[tuple[str | None, datetime, datetime, ExerciseSession]] = []
    for channel, begin, end, row in candidates:
        if any(
            other != channel and overlap_share(begin, end, left, right) >= SAME_WORKOUT
            for other, left, right, _ in kept
        ):
            continue
        kept.append((channel, begin, end, row))
    kept.sort(key=lambda item: (item[1], str(item[3].id)))
    return [
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
            "source_code": HEALTH_KIT,
            "provider": APPLE,
        }
        for _, begin, end, row in kept
    ]


async def archive_bounds(
    session: AsyncSession,
    metric: reading.HistoryMetric,
    today: date,
    zone: ZoneInfo,
) -> reading.ArchiveBounds:
    """The local dates of the selected metric's first and last Apple row up to ``today``:
    sleep by the night's end, steps by the sample's start, heart by the resting value."""
    stop = dashboard_window.daily_window(today, 1, zone).stop
    if metric == "sleep":
        column, model = SleepSession.end_time, SleepSession
    elif metric == "steps":
        column, model = Steps.start_time, Steps
    else:
        column, model = RestingHeartRate.timestamp, RestingHeartRate
    earliest, latest = (
        await session.execute(
            select(func.min(column), func.max(column)).where(
                model.provider == APPLE,
                model.is_quarantined.is_(False),
                column < stop,
            )
        )
    ).one()
    with dashboard_window.checked_dates():
        return reading.ArchiveBounds(
            as_utc(earliest).astimezone(zone).date() if earliest else None,
            as_utc(latest).astimezone(zone).date() if latest else None,
        )


async def sources(session: AsyncSession) -> list[dict[str, Any]]:
    """The Health sources the archive has seen (``apple_sources``, bookkeeping kept by the
    archive; never a scan of ``apple_raw_records``), by name."""
    rows = (await session.scalars(select(AppleSource).order_by(AppleSource.source_name))).all()
    return [
        {
            "source_name": row.source_name,
            "device": row.device,
            "first_seen_at": as_utc(row.first_seen_at),
            "last_seen_at": as_utc(row.last_seen_at),
            "last_data_at": as_utc(row.last_data_at) if row.last_data_at is not None else None,
            "data_types": list(row.data_types or []),
            "channels": list(row.channels or []),
            "records": row.records,
        }
        for row in rows
    ]
