"""Read heart samples against published source signs, before SQL/float rounding."""

import json
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, DecimalException

from sqlalchemy import Select, String, Subquery, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import normalization as numeric
from app.core.providers import GARMIN, garmin_record_key
from app.core.timeutil import as_utc
from app.models.garmin import GarminHeartSet, GarminRawRecord, GarminRecordObservation
from app.models.health import HeartRate
from app.services import shared

from .garmin_versions import VERSION, current_row

_SAMPLE_BATCH = 1000
# Rows requested per fetch. On PostgreSQL SQLAlchemy's asyncpg cursor fetches at
# least 50 rows whatever yield_per asks, so up to 50 raw bodies are decoded at once.
_RAW_BATCH = 16


def _completed_input(sources: Subquery):
    """The source/id index resolves even an empty completed set before legacy data."""
    return (
        select(GarminHeartSet.raw_record_id)
        .where(GarminHeartSet.source_record_id == sources.c.source_record_id)
        .order_by(GarminHeartSet.id.desc())
        .limit(1)
        .correlate(sources)
        .scalar_subquery()
    )


def _legacy_input(sources: Subquery):
    """Probe one source's versions and sightings through their existing indexes.

    Keep kind/key bare on the indexed side; the prefix belongs to the source only.
    Rank observations before unobserved versions, breaking equal times by sighting
    id. With no sightings at all, the maximum raw id wins. This is the legacy
    current_record_ids rule, restricted to this key *before* ranking: no complete
    current-id relation is recomputed or scanned for each distinct source.

    Calendar keys deliberately have no date bounds: sample times can lie elsewhere.
    The caller's COALESCE evaluates this probe only without a completed heart set.
    """
    raw, observation = GarminRawRecord, GarminRecordObservation
    prefix = "heart-rate:"
    return (
        select(raw.id)
        .outerjoin(observation, observation.raw_record_id == raw.id)
        .where(
            func.substr(sources.c.source_record_id, 1, len(prefix)) == prefix,
            raw.kind == "heart-rate",
            raw.key == func.substr(sources.c.source_record_id, len(prefix) + 1),
        )
        .order_by(
            observation.observed_at.desc().nulls_last(),
            observation.id.desc().nulls_last(),
            raw.id.desc(),
        )
        .limit(1)
        .correlate(sources)
        .scalar_subquery()
    )


def _raw_samples(query: Select):
    """Resolve small input ids once, then stream only their sample arrays.

    Materialization on PostgreSQL prevents substitution of the correlated probes
    into a raw join under low normalized-row estimates. Both completed and legacy
    lookups end at an indexed source/key; the outer read matches raw primary keys.
    IN retains one body even if historical source links name the same raw record.
    """
    # Group before resolving inputs: a day's document is read once, not per sample.
    sources = (
        query.with_only_columns(HeartRate.source_record_id, maintain_column_froms=True)
        .order_by(None)
        .distinct()
        .subquery()
    )
    raw = GarminRawRecord
    inputs = (
        select(func.coalesce(_completed_input(sources), _legacy_input(sources)).label("id"))
        .select_from(sources)
        .cte("heart_inputs", nesting=True)
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    # Return just this array as text; default JSON floats can erase source signs.
    return select(raw.key, sql_cast(raw.body.op("->")("heartRateValues"), String)).where(
        raw.id.in_(select(inputs.c.id))
    )


def _negative_keys(key: str, samples: object) -> Iterator[str]:
    """Use the writer's first *prepared* choice, then inspect its unrounded sign.

    An overflowing/invalid candidate cannot reserve a timestamp. A prepared negative
    does reserve it, so a later positive duplicate must never rescue that sample.
    """
    # Keep integer tokens as int for the writer's timestamp contract. Decimal
    # floats retain measurement signs without accepting fractional timestamps.
    samples = json.loads(samples, parse_float=Decimal) if isinstance(samples, str) else None
    if not isinstance(samples, list):
        return
    seen: set[int] = set()
    for sample in samples:
        if not isinstance(sample, list) or len(sample) < 2:
            continue
        stamp = sample[0]
        if not isinstance(stamp, int) or numeric.instant_ms(stamp) is None or stamp in seen:
            continue
        try:
            value = numeric.number(sample[1])
            if value is None:
                continue
            negative = value < 0
            numeric.prepare_numeric(HeartRate, {"bpm": value})
        except DecimalException:
            continue
        seen.add(stamp)
        if negative:
            yield garmin_record_key("heart-rate", key, str(stamp))


async def heart_values(
    session: AsyncSession, start: datetime, stop: datetime, provider: str
) -> list[tuple[datetime, float]]:
    """Two bounded streams; only the small returned samples survive each raw batch.

    Completed membership selects immutable raw inputs even while a newer observation
    awaits normalization. Pre-membership archives retain observation/max-id fallback
    and v1/v2 selection. Normalized-only rows still receive the finite/sign checks.
    """
    query = select(HeartRate.google_data_point_id, HeartRate.timestamp, HeartRate.bpm).where(
        HeartRate.provider == provider,
        current_row(HeartRate),
        HeartRate.is_quarantined.is_(False),
        HeartRate.timestamp >= start,
        HeartRate.timestamp < stop,
    )
    rows = await session.stream(
        query.order_by(HeartRate.timestamp).execution_options(yield_per=_SAMPLE_BATCH)
    )
    found: dict[str, tuple[datetime, float]] = {}
    try:
        async for key, moment, bpm in rows.tuples():
            if not bpm.is_finite() or bpm < 0:
                continue
            value = shared.finite_math.finite_number(bpm)
            if value is not None:
                found[key] = (as_utc(moment), value)
    finally:
        await rows.close()
    if provider == GARMIN and found:
        rows = await session.stream(_raw_samples(query).execution_options(yield_per=_RAW_BATCH))
        try:
            async for key, samples in rows.tuples():
                for negative in _negative_keys(key, samples):
                    found.pop(negative, None)
                    found.pop(f"{negative}:{VERSION}", None)
        finally:
            await rows.close()
    return list(found.values())
