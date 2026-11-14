"""Read current Google weight measurements without reviving preserved archive columns."""

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.core.records import RecordType
from app.models.health import Weight
from app.models.state import RawHealthData
from app.parsers.common import number, payload_for, temporal
from app.services.shared.finite_math import finite_scale


def current_weight(record: object) -> tuple[datetime, float] | None:
    """Use ingestion's payload, value and time parsers, then finite unit conversion.

    Invalid corrections can leave a Weight row in the archive. Its kilograms and
    timestamp are evidence of an older payload, never fallback measurement fields.
    Eligibility (quarantine, deletion and identity) remains the caller's responsibility.
    Numeric strings and measured zero follow the production parser's existing rules.
    """
    if not isinstance(record, dict):
        return None
    spec = RecordType("weight")
    grams = number(payload_for(spec, record).get("weightGrams"))
    timestamp, _, _ = temporal(spec, record)
    if grams is None or timestamp is None:
        return None
    # Check the finite source Decimal before scaling: a tiny negative can round
    # to signed zero in storage or underflow during float conversion. Signed
    # source zero remains valid; malformed/nonfinite numbers keep their policy.
    if grams.is_finite() and grams < 0:
        return None
    kilograms = finite_scale(grams, 1, 1000)
    return (timestamp, kilograms) if kilograms is not None else None


async def latest_weight(
    session: AsyncSession, stop: datetime, eligibility: ColumnElement[bool]
) -> tuple[datetime, float | None] | None:
    """Stream the latest valid time group; a missing value denotes conflicting values.

    Only memory per batch is bounded; the scan has no total cap. It reaches older rows
    only past invalid corrections, and a cap would hide the valid measurement behind
    them. At worst it reads every eligible weight row before stop, one batch at a time.
    Source times are refreshed by store_records even when normalization is skipped.
    Ordering and the upper bound must use those times, so an invalid correction cannot
    retain its old position or hide another valid Google/manual measurement.
    Keep the first valid candidate's source selection, then compare every valid value
    from that source at the same instant. Neither duplicates nor conflicts need a list.
    """
    raw = aliased(RawHealthData)  # eligibility probes the raw table itself
    candidates = await session.stream_scalars(
        select(raw.raw_json)
        .select_from(Weight)
        .join(
            raw,
            and_(
                raw.data_type == "weight",
                raw.google_data_point_id == Weight.google_data_point_id,
            ),
        )
        .where(raw.source_start_time < stop, Weight.is_quarantined.is_(False), eligibility)
        .order_by(raw.source_start_time.desc(), raw.google_data_point_id.desc())
        .execution_options(yield_per=100)
    )
    latest = None
    source = None
    try:
        async for record in candidates:
            measurement = current_weight(record)
            if measurement is None or measurement[0] >= stop:
                continue
            if latest is None:
                latest = measurement
                source = record.get("dataSource")
            elif measurement[0] != latest[0]:
                break
            elif record.get("dataSource") == source and measurement[1] != latest[1]:
                latest = (latest[0], None)
    finally:
        await candidates.close()
    return latest
