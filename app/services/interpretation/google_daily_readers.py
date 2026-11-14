"""Google civil-day reads, including identity, correction and deletion eligibility."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.normalization import first_number
from app.core.records import RecordType
from app.models.eligibility import raw_counted
from app.models.health import SleepSession, Steps
from app.models.identity import plan_each_query
from app.models.state import RawHealthData
from app.parsers.common import number, parse_date, payload_for
from app.parsers.google_daily import DAILY_SPO2_FIELDS, daily_spo2
from app.services.identities.source_precedence import google_source
from app.services.readers import (
    CalendarRead,
    DailyRead,
    DashboardSources,
    DashboardWindow,
    HistoryMetric,
    checked_dates,
    daily_window,
)
from app.services.readers.measurement_windows import (
    interval_window,
    sleep_window,
    step_window,
)
from app.services.readers.reading import ArchiveBounds, DailyValue
from app.services.shared.finite_math import finite_number

from .sleep_estimate import SleepAccumulator
from .step_totals import StepSweep, StepTotals

_STEP_BATCH = 1000
_SUMMARY_BATCH = 1000
_SLEEP_BATCH = 100


def platform_read(reads: dict[str, DailyRead], record: object) -> tuple[str, DailyRead]:
    """Keep one observation stream per platform, including unspecified Google records."""
    source = _platform_source(record)
    return source, reads.setdefault(source, DailyRead({}))


def _platform_source(record: object) -> str:
    data_source = record.get("dataSource") if isinstance(record, dict) else None
    return google_source(data_source.get("platform") if isinstance(data_source, dict) else None)


async def read_steps(
    session: AsyncSession,
    window: DashboardWindow,
    reads: dict[str, DailyRead],
    *,
    calendar: CalendarRead,
):
    totals = StepTotals(calendar.end_date)
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
    try:
        async for record in steps:
            allocation = step_window(record, window.start, window.stop)
            if allocation is None:
                continue
            source, _ = platform_read(reads, record)
            for local, term in allocation.hourly_terms(calendar.zone):
                totals.add(source, local, term)
    finally:
        # Own only the cursor, including on failure or cancellation; the caller
        # retains the session and transaction. Exhaustion also needs explicit close.
        await steps.close()
    for day, source, value in totals.finish(date.max.toordinal() + 1):
        reads[source].days.setdefault(day, {})["steps"] = DailyValue(value, source, "GOOGLE")
    for source, buckets in totals.buckets.items():
        reads[source] = DailyRead(
            reads[source].days,
            [buckets[h].result() if h in buckets else None for h in range(24)],
        )


async def read_sleep(
    session: AsyncSession,
    window: DashboardWindow,
    zone: ZoneInfo,
    sources: DashboardSources,
    reads: dict[str, DailyRead],
):
    async with aclosing(_sleep_days(session, window, zone, sources)) as days:
        async for day, measurement in days:
            read = reads.setdefault(measurement.source, DailyRead({}))
            read.days.setdefault(day, {})["sleep_seconds"] = measurement


def _finish_sleep_days(
    days: dict[date, dict[str, SleepAccumulator]], before: int
) -> Iterator[tuple[date, DailyValue]]:
    """Release completed days, including their events, before yielding measurements."""
    for day in sorted(days):
        if day.toordinal() >= before:
            break
        accumulators = days.pop(day)
        for source in tuple(accumulators):
            estimate = accumulators.pop(source).result()
            yield day, DailyValue(estimate.seconds, source, "GOOGLE", tuple(estimate.assumptions))


def _sleep_query(window: DashboardWindow, sources: DashboardSources):
    """Current raw wake-window candidates under the daily eligibility policy."""
    # Each session's own raw record (same key; storage keys both alike) carries the shape
    # asleep time is read from, so rows normalized before a rule change need no rewrite.
    # Joined under an alias: the eligibility subqueries must not correlate to it.
    sleep_raw = aliased(RawHealthData)
    return (
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


async def _sleep_days(
    session: AsyncSession, window: DashboardWindow, zone: ZoneInfo, sources: DashboardSources
) -> AsyncGenerator[tuple[date, DailyValue], None]:
    """Shared eligibility, wake days and per-platform estimates in bounded batches.

    UTC end order need not imply local date order: a rollback can cross midnight.
    Offsets are strictly within +/-24 hours, so at UTC day D no later session
    can wake before D-1. Only up to three civil days need overlap events; completed
    days and raw documents never accumulate across the archive. No per-day query.
    Consumers must close this iterator, including when processing a yield fails.
    """
    sleep = await session.stream_scalars(
        _sleep_query(window, sources),
        execution_options={"yield_per": _SLEEP_BATCH},
    )
    by_day: dict[date, dict[str, SleepAccumulator]] = {}
    try:
        async for raw_json in sleep:
            measurement = sleep_window(raw_json, window.start, window.stop)
            if measurement is None:
                continue
            span, payload = measurement
            for value in _finish_sleep_days(by_day, span.end_time.date().toordinal() - 1):
                yield value
            with checked_dates():
                day = span.end_time.astimezone(zone).date()
            if window.first <= day < window.following:
                accumulators = by_day.setdefault(day, {})
                source = _platform_source(raw_json)
                if source not in accumulators:
                    accumulators[source] = SleepAccumulator()
                accumulators[source].add(span, payload)
                del accumulators  # do not retain a completed day's events across yields
    finally:
        # Own only the cursor: the caller retains its session and transaction.
        await sleep.close()
    for value in _finish_sleep_days(by_day, date.max.toordinal() + 1):
        yield value


async def read_summaries(
    session: AsyncSession,
    window: DashboardWindow,
    sources: DashboardSources,
    reads: dict[str, DailyRead],
):
    async with aclosing(
        _summary_candidates(session, window.first, window.following, sources)
    ) as daily:
        async for day, key, measurement in daily:
            read = reads.setdefault(measurement.source, DailyRead({}))
            read.days.setdefault(day, {})[key] = measurement


_SUMMARIES = {
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
    "daily-oxygen-saturation": ("spo2", DAILY_SPO2_FIELDS),
}


async def _summary_candidates(
    session: AsyncSession, first: date, following: date, sources: DashboardSources
):
    """Stream decisive candidates in day order, then by recency and id within each day."""
    metrics = {kind: read for kind, read in _SUMMARIES.items() if read[0] in sources.metrics}
    if not metrics:
        return
    # Only payloads and their kinds are needed; keep no ORM objects or candidate
    # lists. yield_per bounds decoding even when many candidates share a day.
    daily = await session.stream(
        select(RawHealthData.data_type, RawHealthData.raw_json)
        .where(
            RawHealthData.data_type.in_(metrics),
            RawHealthData.source_start_time >= datetime.combine(first, time.min, UTC),
            RawHealthData.source_start_time < datetime.combine(following, time.min, UTC),
            sources.raw(tuple(metrics)),
        )
        # Storage derives source_start_time from the payload's civil date at UTC
        # midnight. Group days while preserving the existing candidate order within
        # each day/platform, even when a day has more candidates than the batch.
        .order_by(
            RawHealthData.source_start_time,
            RawHealthData.last_seen_at,
            RawHealthData.google_data_point_id,
        ),
        execution_options={"yield_per": _SUMMARY_BATCH},
    )
    try:
        async for kind, record in daily:
            payload = payload_for(RecordType(kind), record)
            day = parse_date(payload.get("date"))
            if day is not None and first <= day < following:
                key, fields = metrics[kind]
                value = (
                    daily_spo2(payload)[1]
                    if key == "spo2"
                    else first_number(payload, fields, parse=number)
                )
                # Unparseable candidates leave the prior value; parsed nonfinite
                # or negative candidates replace it with None. Validate the source
                # Decimal before float conversion, retaining true and signed zero.
                if value is not None:
                    measured = None if value.is_finite() and value < 0 else finite_number(value)
                    yield day, key, DailyValue(measured, _platform_source(record), "GOOGLE")
    finally:
        # Exhaustion alone does not close an interrupted server-side cursor.
        await daily.close()


async def daily_values(
    session: AsyncSession,
    window: DashboardWindow,
    end_date: date,
    zone: ZoneInfo,
    sources: DashboardSources,
) -> tuple[DailyRead, ...]:
    """Read requested metrics separately per platform for the shared policy resolver."""
    reads: dict[str, DailyRead] = {}
    if "steps" in sources.metrics:
        await read_steps(
            session,
            window,
            reads,
            calendar=CalendarRead(end_date=end_date, zone=zone, sources=sources),
        )
    if "sleep_seconds" in sources.metrics:
        await read_sleep(session, window, zone, sources, reads)
    await read_summaries(session, window, sources, reads)
    return tuple(reads[source] for source in sorted(reads))


async def archive_bounds(
    session: AsyncSession, metric: HistoryMetric, today: date, zone: ZoneInfo
) -> ArchiveBounds:
    """Eligible Google dates supported by current daily measurements."""
    await plan_each_query(session)
    if metric == "steps":
        return await _step_archive_bounds(session, today, zone)
    if metric == "heart":
        return await _heart_archive_bounds(session, today)
    return await _sleep_archive_bounds(session, today, zone)


async def _sleep_archive_bounds(
    session: AsyncSession, today: date, zone: ZoneInfo
) -> ArchiveBounds:
    """Reduce the shared daily stream to extrema, retaining no archive-wide results."""
    end = daily_window(today, 1, zone)
    window = DashboardWindow(date.min, end.following, datetime.min.replace(tzinfo=UTC), end.stop)
    earliest = latest = None
    async with aclosing(_sleep_days(session, window, zone, DashboardSources())) as days:
        async for day, measurement in days:
            # One invalid session invalidates its platform's day. Another platform
            # can still support the bounds, including a measured zero.
            if measurement.value is not None:
                earliest = min(earliest, day) if earliest is not None else day
                latest = max(latest, day) if latest is not None else day
    return ArchiveBounds(earliest, latest)


async def _step_archive_bounds(session: AsyncSession, today: date, zone: ZoneInfo) -> ArchiveBounds:
    """Only representable per-platform daily totals establish contribution days."""
    earliest = latest = None
    async with aclosing(_step_days(session, today, zone)) as days:
        async for day, measurement in days:
            if measurement.value is not None:
                earliest = min(earliest, day) if earliest is not None else day
                latest = max(latest, day) if latest is not None else day
    return ArchiveBounds(earliest, latest)


async def _step_days(session: AsyncSession, today: date, zone: ZoneInfo):
    start = datetime.min.replace(tzinfo=UTC)
    stop = daily_window(today, 1, zone).stop
    records = await session.stream_scalars(
        select(RawHealthData.raw_json)
        .where(
            RawHealthData.data_type == "steps",
            raw_counted(data_types=("steps",)),
            RawHealthData.source_start_time < stop,
            RawHealthData.source_end_time > RawHealthData.source_start_time,
            select(Steps.id)
            .where(
                Steps.google_data_point_id == RawHealthData.google_data_point_id,
                Steps.is_quarantined.is_(False),
            )
            .exists(),
        )
        .order_by(RawHealthData.source_start_time, RawHealthData.google_data_point_id),
        execution_options={"yield_per": _STEP_BATCH},
    )
    sweep = StepSweep(zone)
    try:
        with checked_dates():
            async for record in records:
                # Current payload validation, including corrections with retained old
                # Steps rows, stays identical to the daily reader's step_window path.
                allocation = step_window(record, start, stop)
                if allocation is None:
                    continue
                for day, source, value in sweep.advance(allocation.start):
                    yield day, DailyValue(value, source, "GOOGLE")
                sweep.add(_platform_source(record), allocation)
    finally:
        # Close on exhaustion, processing/consumer failure and cancellation, while
        # leaving the caller's session and transaction intact.
        await records.close()
    with checked_dates():
        for day, source, value in sweep.advance(None):
            yield day, DailyValue(value, source, "GOOGLE")


async def _heart_archive_bounds(session: AsyncSession, today: date) -> ArchiveBounds:
    sources = DashboardSources(metrics=("resting_bpm",))
    following = daily_window(today, 1, ZoneInfo("UTC")).following
    earliest = latest = current = None
    values: dict[str, float | None] = {}
    # No all-archive DailyRead or list of documents: only a batch of payloads and
    # the current day's platform values survive. aclosing also covers consumer errors.
    async with aclosing(_summary_candidates(session, date.min, following, sources)) as daily:
        async for day, _, measurement in daily:
            if day != current:
                if current is not None and any(value is not None for value in values.values()):
                    earliest = earliest or current
                    latest = current
                current = day
                values.clear()
            # A decisive invalid correction hides this platform's prior value, but
            # cannot erase another platform's valid candidate. Zero is a measurement.
            values[measurement.source] = measurement.value
    if current is not None and any(value is not None for value in values.values()):
        earliest = earliest or current
        latest = current
    return ArchiveBounds(earliest, latest)
