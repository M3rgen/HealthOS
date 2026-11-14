"""Read daily steps without letting SQL rounding erase a negative source sign."""

import json
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

from sqlalchemy import String, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import GARMIN
from app.models.garmin import GarminRawRecord
from app.models.health import Steps
from app.parsers.common import number
from app.services import shared

from .garmin_daily_input import daily_input_rows
from .garmin_versions import current_row


def negative_steps(value: object) -> bool:
    """Compare the source Decimal before float underflow; signed zero is valid."""
    parsed = number(value)
    return parsed is not None and parsed.is_finite() and parsed < 0


async def step_values(
    session: AsyncSession, first: date, last: date, provider: str = GARMIN
) -> AsyncIterator[tuple[str, float]]:
    """Yield current, non-quarantined measurements and retain normalized numeric bounds.

    A completed projection owns its immutable input, even while a newer observation
    awaits normalization. Only archives without projection evidence use the latest
    observed raw version (or legacy max id). Normalized-only archives remain readable.
    Neither an old successful input nor an old normalized value can rescue a negative.
    """
    raw = GarminRawRecord
    eligible = select(Steps.source_record_id, Steps.count, Steps.provider).where(
        Steps.provider == provider,
        current_row(Steps),
        Steps.is_quarantined.is_(False),
        Steps.source_record_id >= f"steps:{first.isoformat()}",
        Steps.source_record_id <= f"steps:{last.isoformat()}",
    )
    inputs = daily_input_rows(eligible, Steps, "steps", ("steps",), first, last).c
    # Return JSON text so the driver cannot underflow a numeric token to -0.0.
    # Decode just this member, never the entire daily body.
    source_steps = sql_cast(raw.body.op("->")("totalSteps"), String)
    query = select(inputs.source_record_id, inputs.count, source_steps).outerjoin(
        raw, (inputs.provider == GARMIN) & (raw.id == inputs.input_0)
    )
    rows = await session.stream(query.execution_options(yield_per=1000))
    try:
        async for key, count, source in rows.tuples():
            if provider == GARMIN:
                source = (
                    json.loads(source, parse_float=Decimal, parse_int=Decimal)
                    if isinstance(source, str)
                    else None
                )
                if negative_steps(count) or negative_steps(source):
                    continue
            value = shared.finite_math.finite_number(count)
            if key is not None and value is not None:
                yield key, value
    finally:
        await rows.close()
