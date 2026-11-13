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


def current_heart_rate(record: dict) -> tuple[datetime, float] | None:
    """Validate both time and value without borrowing retained normalized columns.

    Storage intentionally retains HeartRate when a correction cannot normalize. It
    remains archive evidence, rather than a fallback measurement for a newer payload.
    Use the ingestion parser's payload keys, temporal forms and numeric field aliases;
    zero is a supplied value. Identity, quarantine and deletion belong to the query.
    """
    spec = RecordType("heart-rate")
    timestamp, _, _ = temporal(spec, record)
    bpm = finite_number(
        first_number(payload_for(spec, record), ("beatsPerMinute", "bpm", "value"), parse=number)
    )
    if timestamp is None or bpm is None:
        return None
    return timestamp, bpm


async def heart_rate_samples(
    session: AsyncSession,
    start: datetime,
    stop: datetime,
    source_filter: ColumnElement[bool],
) -> list[tuple[datetime, float, object]]:
    """The current heart-rate samples in ``[start, stop)`` with their ``dataSource.platform``
    (for the source label), under the caller's eligibility predicate; time-ordered."""
    raw = aliased(RawHealthData)
    records = await session.scalars(
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
    )
    found = []
    for record in records:
        measurement = current_heart_rate(record)
        if measurement is None:
            continue
        timestamp, bpm = measurement
        source = record.get("dataSource") if isinstance(record, dict) else None
        found.append((timestamp, bpm, source.get("platform") if isinstance(source, dict) else None))
    return found
