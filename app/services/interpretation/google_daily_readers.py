"""Google civil-day reads, including identity, correction and deletion eligibility."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.normalization import first_number
from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.eligibility import raw_counted
from app.models.health import SleepSession, Steps
from app.models.identity import plan_each_query
from app.models.state import RawHealthData
from app.parsers.common import number, parse_date, payload_for
from app.schemas.dashboard import DashboardDay
from app.services.identities.source_precedence import google_source_of
from app.services.readers import (
    CalendarRead,
    DailyRead,
    DashboardSources,
    DashboardWindow,
    HistoryMetric,
    checked_dates,
    daily_window,
    day_rows,
)
from app.services.readers.dashboard_rows import DAILY_METRICS, Platforms
from app.services.readers.measurement_windows import (
    interval_window,
    sleep_window,
    step_window,
)
from app.services.readers.reading import ArchiveBounds, DailyValue
from app.services.shared.finite_math import finite_number, finite_scaled_sum

from .sleep_estimate import sleep_estimate

_STEP_BATCH = 1000


def seen(platforms: Platforms, day: date, metric: str, record: object) -> None:
    source = record.get("dataSource") if isinstance(record, dict) else None
    platform = source.get("platform") if isinstance(source, dict) else None
    platforms[day].setdefault(metric, set()).add(platform if isinstance(platform, str) else None)


async def read_steps(
    session: AsyncSession,
    window: DashboardWindow,
    rows: dict[date, DashboardDay],
    platforms: Platforms,
    *,
    calendar: CalendarRead,
):
    step_days = {}
    step_buckets = {}
    # Split source intervals at civil-hour boundaries, including midnight and DST.
    step_raw = aliased(RawHealthData)  # the eligibility filter probes the raw table itself
    # Streamed: a month of minute records is tens of thousands of decoded JSON documents, and
    # only their sums are kept.
    steps = await session.stream_scalars(
        select(step_raw.raw_json)
        .select_from(Steps)
        .join(
            step_raw,
            and_(
                step_raw.data_type == "steps",
                step_raw.google_data_point_id == Steps.google_data_point_id,
            ),
        )
        .where(
            *interval_window(
                "steps",
                step_raw.source_start_time,
                step_raw.source_end_time,
                window.start,
                window.stop,
            ),
            Steps.is_quarantined.is_(False),
            calendar.sources.normalized(Steps, "steps"),
        ),
        execution_options={"yield_per": _STEP_BATCH},
    )
    async for record in steps:
        allocation = step_window(record, window.start, window.stop)
        if allocation is None:
            continue
        for local, term in allocation.hourly_terms(calendar.zone):
            day = local.date()
            step_days.setdefault(day, []).append(term)
            if day in platforms:
                seen(platforms, day, "steps", record)
            if day == calendar.end_date:
                step_buckets.setdefault(local.hour, []).append(term)
    # Evaluate whole buckets so cancellation can recover overflowing intermediates.
    for day, terms in step_days.items():
        value = finite_scaled_sum(terms)
        rows[day]["steps"] = round(value) if value is not None else None
    return [finite_scaled_sum(step_buckets[h]) if h in step_buckets else None for h in range(24)]


async def read_sleep(
    session: AsyncSession,
    window: DashboardWindow,
    zone: ZoneInfo,
    sources: DashboardSources,
    rows: dict[date, DashboardDay],
    platforms: Platforms,
):
    # Each session's own raw record (same key; storage keys both alike) carries the shape
    # asleep time is read from, so rows normalized before a rule change need no rewrite.
    # Joined under an alias: the eligibility subqueries must not correlate to it.
    sleep_raw = aliased(RawHealthData)
    sleep = (
        await session.execute(
            select(sleep_raw.raw_json)
            .select_from(SleepSession)
            .join(
                sleep_raw,
                and_(
                    sleep_raw.data_type == "sleep",
                    sleep_raw.google_data_point_id == SleepSession.google_data_point_id,
                ),
            )
            .where(
                *interval_window(
                    "sleep",
                    sleep_raw.source_start_time,
                    sleep_raw.source_end_time,
                    window.start,
                    window.stop,
                ),
                SleepSession.is_quarantined.is_(False),
                sources.normalized(SleepSession, "sleep"),
            )
            .order_by(sleep_raw.source_end_time)
        )
    ).all()
    by_day = {}
    for (raw_json,) in sleep:
        measurement = sleep_window(raw_json, window.start, window.stop)
        if measurement is None:
            continue
        span, payload = measurement
        day = span.end_time.astimezone(zone).date()
        if day in rows:
            by_day.setdefault(day, []).append((span, payload))
            seen(platforms, day, "sleep_seconds", raw_json)
    for day, values in rows.items():
        estimate = sleep_estimate(by_day.get(day, []))
        values["sleep_seconds"] = estimate.seconds
        values["sleep_assumptions"] = estimate.assumptions


async def read_summaries(
    session: AsyncSession,
    window: DashboardWindow,
    sources: DashboardSources,
    rows: dict[date, DashboardDay],
    platforms: Platforms,
):
    # Daily summaries have civil dates: do not average them with intraday samples.
    summaries = {
        "daily-resting-heart-rate": ("resting_bpm", ("beatsPerMinute",)),
        "daily-heart-rate-variability": (
            "hrv_ms",
            (
                "averageHeartRateVariabilityMilliseconds",
                "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "rmssd",
            ),
        ),
        "daily-oxygen-saturation": ("spo2", ("averagePercentage", "percentage", "average")),
    }
    metrics = {kind: read for kind, read in summaries.items() if read[0] in sources.metrics}
    if not metrics:
        return
    daily = (
        await session.scalars(
            select(RawHealthData)
            .where(
                RawHealthData.data_type.in_(metrics),
                RawHealthData.source_start_time >= datetime.combine(window.first, time.min, UTC),
                RawHealthData.source_start_time < datetime.combine(window.following, time.min, UTC),
                sources.raw(tuple(metrics)),
            )
            .order_by(RawHealthData.last_seen_at, RawHealthData.google_data_point_id)
        )
    ).all()
    for item in daily:
        payload = payload_for(RecordType(item.data_type), item.raw_json)
        day = parse_date(payload.get("date"))
        if day is not None and day in rows:
            key, fields = metrics[item.data_type]
            value = first_number(payload, fields, parse=number)
            if value is not None:
                rows[day][key] = finite_number(value)
                seen(platforms, day, key, item.raw_json)


async def daily_values(
    session: AsyncSession,
    window: DashboardWindow,
    end_date: date,
    zone: ZoneInfo,
    sources: DashboardSources,
) -> DailyRead:
    """Reads only the records of ``sources.metrics``; the other values stay ``None``."""
    rows, platforms = day_rows(window.first, (end_date - window.first).days + 1, end_date)
    hours = None
    if "steps" in sources.metrics:
        hours = await read_steps(
            session,
            window,
            rows,
            platforms,
            calendar=CalendarRead(end_date=end_date, zone=zone, sources=sources),
        )
    if "sleep_seconds" in sources.metrics:
        await read_sleep(session, window, zone, sources, rows, platforms)
    await read_summaries(session, window, sources, rows, platforms)
    return DailyRead(
        {
            day: {
                metric: DailyValue(
                    values[metric],
                    google_source_of(platforms[day].get(metric, ())),
                    "GOOGLE",
                    tuple(values["sleep_assumptions"]) if metric == "sleep_seconds" else (),
                )
                for metric in DAILY_METRICS
            }
            for day, values in rows.items()
        },
        hours,
    )


async def archive_bounds(
    session: AsyncSession, metric: HistoryMetric, today: date, zone: ZoneInfo
) -> ArchiveBounds:
    """Eligible Google archive dates without loading payloads or unrelated metrics."""
    source_type = {"steps": "steps", "sleep": "sleep", "heart": "daily-resting-heart-rate"}[metric]
    # Daily heart summaries encode civil dates at UTC midnight; sleep uses the wake day.
    date_zone = ZoneInfo("UTC") if metric == "heart" else zone
    source_time = (
        RawHealthData.source_end_time if metric == "sleep" else RawHealthData.source_start_time
    )
    stop = daily_window(today, 1, date_zone).stop
    await plan_each_query(session)
    bounds = select(func.min(source_time), func.max(source_time)).where(
        RawHealthData.data_type == source_type,
        raw_counted(data_types=(source_type,)),
        source_time < stop,
    )
    if metric != "heart":
        model = Steps if metric == "steps" else SleepSession
        bounds = bounds.where(
            select(model.id)
            .where(
                model.google_data_point_id == RawHealthData.google_data_point_id,
                model.is_quarantined.is_(False),
            )
            .exists()
        )
    if metric == "sleep":
        # Partial corrections cannot establish an archive wake day. Use current raw
        # timestamps, never the retained normalized row's historical interval.
        bounds = bounds.where(
            RawHealthData.source_start_time.is_not(None),
            RawHealthData.source_end_time > RawHealthData.source_start_time,
        )
    earliest, latest = (await session.execute(bounds)).one()
    with checked_dates():
        return ArchiveBounds(
            as_utc(earliest).astimezone(date_zone).date() if earliest else None,
            as_utc(latest).astimezone(date_zone).date() if latest else None,
        )
