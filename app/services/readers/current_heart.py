"""Bounded heart-rate chart reads from the current source payload."""

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.core.normalization import first_number
from app.core.records import RecordType
from app.models.health import HeartRate
from app.models.state import RawHealthData
from app.parsers.common import number, payload_for, temporal
from app.services.shared.finite_math import finite_number

from . import heart_projection

# Bound decoded payload count; an individual Google document can still be large.
_HEART_BATCH = 100


def current_heart_rate(record: dict) -> tuple[datetime, float] | None:
    """Validate both time and value without borrowing retained normalized columns.

    Storage intentionally retains HeartRate when a correction cannot normalize. It
    remains archive evidence, rather than a fallback measurement for a newer payload.
    Use the ingestion parser's payload keys, temporal forms and numeric field aliases;
    zero is a supplied value. Identity, quarantine and deletion belong to the query.
    """
    spec = RecordType("heart-rate")
    timestamp, _, _ = temporal(spec, record)
    parsed = first_number(
        payload_for(spec, record), ("beatsPerMinute", "bpm", "value"), parse=number
    )
    # Reject the selected Decimal before float conversion, including tiny negatives.
    # Do not borrow a positive alias; signed zero remains a supplied measurement.
    if parsed is not None and parsed.is_finite() and parsed < 0:
        return None
    bpm = finite_number(parsed)
    if timestamp is None or bpm is None:
        return None
    return timestamp, bpm


async def heart_rate_samples(
    session: AsyncSession,
    start: datetime,
    stop: datetime,
    source_filter: ColumnElement[bool],
) -> list[tuple[datetime, float, object]]:
    """Stream current samples in ``[start, stop)`` under the caller's eligibility predicate.

    Keep source-time ordering and ``dataSource.platform`` for the caller's source labels.
    Invalid corrections are parsed from the current payload, never the retained HeartRate
    value. The join and source bounds therefore continue to use the current raw record.

    Only JSON buffering is bounded by the fetch batch; the returned small sample tuples
    still grow with the number of valid readings. There is no total record limit.
    Close this result on completion, processing failure or cancellation; the caller
    retains ownership of the session and its transaction.
    """
    raw = aliased(RawHealthData)
    records = await session.stream_scalars(
        select(raw.raw_json)
        .select_from(HeartRate)
        .join(
            raw,
            and_(
                raw.data_type == "heart-rate",
                raw.google_data_point_id == HeartRate.google_data_point_id,
            ),
        )
        .where(
            raw.source_start_time >= start,
            raw.source_start_time < stop,
            HeartRate.is_quarantined.is_(False),
            source_filter,
        )
        .order_by(raw.source_start_time)
        .execution_options(yield_per=_HEART_BATCH)
    )
    found = []
    try:
        async for record in records:
            measurement = current_heart_rate(record)
            if measurement is None:
                continue
            timestamp, bpm = measurement
            source = record.get("dataSource") if isinstance(record, dict) else None
            found.append(
                (
                    timestamp,
                    bpm,
                    source.get("platform") if isinstance(source, dict) else None,
                )
            )
    finally:
        await records.close()
    return found


async def chart_heart_rate_samples(
    session: AsyncSession,
    start: datetime,
    stop: datetime,
    source_filter: ColumnElement[bool],
) -> list[tuple[datetime, float, object]]:
    """Reuse current hours, falling back only for each missing or stale UTC hour."""
    cached = await heart_projection.packets(session, start, stop, source_filter)
    if cached is None:
        return await heart_rate_samples(session, start, stop, source_filter)
    found = []
    bucket = heart_projection.hour(start)
    while bucket < stop:
        if bucket in cached:
            found.extend(cached[bucket])
        else:
            found.extend(
                await heart_rate_samples(
                    session,
                    max(start, bucket),
                    min(stop, bucket + heart_projection.HOUR),
                    source_filter,
                )
            )
        bucket += heart_projection.HOUR
    return found
