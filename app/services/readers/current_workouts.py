"""Read Google workouts from the current payload, preserving the normalized archive.

Each row carries its source: ``source_code`` (the record's ``dataSource.platform``, e.g.
``FITBIT``; ``GOOGLE`` when the record names none) and ``provider`` (``GOOGLE``). The
display label and the precedence placement are added by the API (app/api/dashboard.py).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.records import RecordType
from app.models.eligibility import normalized_counted
from app.models.health import ExerciseSession
from app.models.state import RawHealthData
from app.parsers.common import duration_seconds, number, payload_for
from app.services.archive.normalization import normalize
from app.services.identities.source_precedence import google_source

from .reading import WorkoutRecord


def nonnegative_metrics(metrics: object) -> tuple[dict, list[str]]:
    """Project optional Google measurements without changing the source payload.

    Check finite source Decimals before SQL rounding, scaling or float conversion:
    even a tiny negative is unknown, while signed zero is a real measurement.
    Leave malformed/nonfinite fields to each reader's existing numeric policy.
    Field-specific reasons let Umay explain which optional measurement is absent.
    """
    values = dict(metrics) if isinstance(metrics, dict) else {}
    reasons = []
    for field in ("caloriesKcal", "distanceMillimeters"):
        source = number(values.get(field))
        if source is not None and source.is_finite() and source < 0:
            values[field] = None
            reasons.append(f"negative_measurement:{field}")
    return values, reasons


def _display_duration(
    duration: Decimal | None, payload: dict, start: datetime, end: datetime
) -> float | None:
    """Validate a storable duration at source precision, retaining SQL output rounding.

    Normalization already owns activeDuration/duration precedence and numeric
    bounds. Missing durations and optional SQL overflow retain the historical
    interval fallback. A selected, storable duration outside the interval is
    unknown instead: neither an alternative field nor an archived measurement
    may replace it. Measured zero and the exact interval length remain valid.

    After successful normalization, select the source field with the parser's
    precedence: malformed/missing activeDuration permits duration, but a parsed
    zero or invalid measurement does not. Nonfinite numbers already reject the
    session in normalize; optional SQL overflow still uses the interval fallback.

    Build the physical limit exactly from timedelta components. Neither SQL
    rounding nor float total_seconds() may hide a source value beyond that limit.
    This projection never changes the normalized or raw archive, and valid source
    measurements still use the normalized value for the API's output precision.
    """
    delta = end - start
    if duration is None:
        return delta.total_seconds()
    source = duration_seconds(payload.get("activeDuration"))
    if source is None:
        source = duration_seconds(payload.get("duration"))
    if source is None:
        source = duration  # Preserve normalization's missing-duration fallback.
    seconds = delta.days * 86400 + delta.seconds
    interval = Decimal(f"{seconds}.{delta.microseconds:06d}")
    if not 0 <= source <= interval:
        return None
    return float(duration)


def current_workout(identifier: object, record: object) -> WorkoutRecord | None:
    """Apply ingestion's field priorities, numeric limits and optional text contract.

    A failed correction may leave an older ExerciseSession intact. Its id is still
    the public id, but none of its dates or measurements can be used as fallbacks.
    Normalization here only constructs values in memory; it never writes a row.
    """
    if not isinstance(record, dict):
        return None
    spec = RecordType("exercise")
    payload = payload_for(spec, record)
    metrics, _ = nonnegative_metrics(payload.get("metricsSummary"))
    parsed = normalize(spec, {**record, spec.json_key: {**payload, "metricsSummary": metrics}})
    if parsed is None or parsed.model is not ExerciseSession:
        return None
    values = parsed.values
    start, end = values["start_time"], values["end_time"]
    if end <= start:
        return None
    return {
        "id": str(identifier),
        "exercise_type": values["exercise_type"],
        "start_time": start,
        "end_time": end,
        "duration_seconds": _display_duration(values["duration_seconds"], payload, start, end),
        "calories_kcal": (
            float(values["calories_kcal"]) if values["calories_kcal"] is not None else None
        ),
        "distance_meters": (
            float(values["distance_meters"]) if values["distance_meters"] is not None else None
        ),
        "source_code": google_source(_platform(record)),
        "provider": "GOOGLE",
    }


def _platform(record: object) -> object:
    source = record.get("dataSource") if isinstance(record, dict) else None
    return source.get("platform") if isinstance(source, dict) else None


async def current_workouts(
    session: AsyncSession, start: datetime, stop: datetime
) -> list[WorkoutRecord]:
    """Select by current source time, retaining all existing eligibility filters.

    store_records refreshes source times even when normalization fails. Using them
    for the month bounds prevents preserved sessions from retaining their old month.
    The alias keeps normalized_counted's raw-table probes independently correlated.
    Stream payloads in bounded batches so the full raw month need not be held in memory.
    """
    raw = aliased(RawHealthData)
    candidates = await session.stream(
        select(ExerciseSession.id, raw.raw_json)
        .join(
            raw,
            and_(
                raw.data_type == "exercise",
                raw.google_data_point_id == ExerciseSession.google_data_point_id,
            ),
        )
        .where(
            raw.source_start_time >= start,
            raw.source_start_time < stop,
            raw.source_end_time > raw.source_start_time,
            normalized_counted(ExerciseSession),
        )
        .execution_options(yield_per=100)
    )
    rows = []
    try:
        async for identifier, record in candidates:
            row = current_workout(identifier, record)
            if row is not None and start <= row["start_time"] < stop:
                rows.append(row)
    finally:
        await candidates.close()
    return rows
