"""Read daily scalars against committed source signs before SQL/float rounding."""

import json
from collections.abc import AsyncIterator, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Select, String, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.providers import GARMIN
from app.models.garmin import GarminRawRecord
from app.models.health import HeartRateVariability, OxygenSaturation, RestingHeartRate
from app.parsers.common import number
from app.services import shared

from .garmin_daily_input import daily_input_rows
from .garmin_versions import current_row

# Read only these source members, never samples, metrics metadata or whole bodies.
SCALARS = {
    "resting_bpm": (
        RestingHeartRate,
        RestingHeartRate.bpm,
        "resting-heart-rate",
        (("daily-summaries", ("heartRate", "restingValue")), ("heart-rate", ("restingHeartRate",))),
    ),
    "hrv_ms": (
        HeartRateVariability,
        HeartRateVariability.rmssd,
        "hrv",
        (("hrv-summaries", ("lastNightAvg",)), ("hrv", ("hrvSummary", "lastNightAvg"))),
    ),
    "spo2": (
        OxygenSaturation,
        OxygenSaturation.spo2,
        "spo2",
        (("spo2", ("averageSpO2",)),),
    ),
}


def _parsed(value: object) -> Decimal | None:
    # Decode only the selected member, before default JSON floats erase its sign.
    parsed = number(
        json.loads(value, parse_float=Decimal, parse_int=Decimal)
        if isinstance(value, str)
        else None
    )
    return parsed if parsed is not None and parsed.is_finite() else None


def _source_value(metric: str, sources: Sequence[object]) -> Decimal | None:
    # Resting uses the first parseable field. HRV instead chooses a whole record:
    # a nonempty primary with a missing/broken average still blocks the alternate.
    if metric == "hrv_ms":
        primary, present, alternate = sources
        return _parsed(primary if present else alternate)
    primary = _parsed(sources[0])
    if primary is None and metric == "resting_bpm":
        return _parsed(sources[1])
    return primary


def _query(metric: str, first: date, last: date, provider: str) -> Select[tuple[Any, ...]]:
    model, column, kind, inputs = SCALARS[metric]
    eligible = select(model.source_record_id, column, model.provider).where(
        model.provider == provider,
        current_row(model),
        model.is_quarantined.is_(False),
        model.source_record_id >= f"{kind}:{first.isoformat()}",
        model.source_record_id <= f"{kind}:{last.isoformat()}",
    )
    resolved = daily_input_rows(
        eligible, model, kind, [raw_kind for raw_kind, _ in inputs], first, last
    )
    query = select(resolved.c.source_record_id, resolved.c[column.key]).select_from(resolved)
    for index, (_raw_kind, path) in enumerate(inputs):
        raw = aliased(GarminRawRecord, name=f"daily_input_{index}")
        query = query.outerjoin(
            raw, (resolved.c.provider == GARMIN) & (raw.id == resolved.c[f"input_{index}"])
        )
        # Cast the final JSON member to text, bypassing the driver's JSON decoder.
        member = raw.body
        for name in path:
            member = member.op("->")(name)
        query = query.add_columns(sql_cast(member, String))
        if metric == "hrv_ms" and index == 0:
            query = query.add_columns(
                raw.id.is_not(None) & (raw.body != {}) & (raw.body != []) & (raw.body != JSON.NULL)
            )
    return query


async def scalar_values(
    session: AsyncSession, metric: str, first: date, last: date, provider: str = GARMIN
) -> AsyncIterator[tuple[str, float]]:
    """Stream current values; pending observations cannot replace published inputs.

    Without projection evidence, retain observation/legacy version choice. Without
    raw evidence, keep normalized-only rows readable. Never rescue a negative from
    an older version or an alternate field, and retain the existing numeric bounds.
    """
    rows = await session.stream(
        _query(metric, first, last, provider).execution_options(yield_per=1000)
    )
    try:
        async for key, normalized, *sources in rows.tuples():
            if provider == GARMIN:
                source = _source_value(metric, sources)
                if (source is not None and source < 0) or (
                    normalized is not None and normalized < 0
                ):
                    continue
            value = shared.finite_math.finite_number(normalized)
            if key is not None and value is not None:
                yield key, value
    finally:
        await rows.close()
