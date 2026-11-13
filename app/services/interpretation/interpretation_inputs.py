"""Bounded, eligible source inputs and evidence-backed device continuity."""

import asyncio
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.normalization import number
from app.core.timeutil import as_utc
from app.models.eligibility import raw_counted
from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.state import RawHealthData
from app.parsers.common import canonical_json, payload_hash
from app.parsers.common import number as loose_number
from app.parsers.identity import stable_source
from app.services.archive.manual_flags import excluded_measurements
from app.services.shared.finite_math import finite_scale
from app.services.shared.query_ranges import CivilDateRange

from .health_windows import input_bounds
from .interpretation_data import EvidenceData
from .interpretation_input_state import input_rows, measurement_flags, source_decisions

MAX_INPUT_RECORDS = 50_000
MAX_INPUT_BYTES = 16 * 1024 * 1024


def scaled_measurement(
    value: object, multiplier: float, divisor: float = 1
) -> tuple[float | None, list[str]]:
    """Validate a source number in its final unit, without an early float cast.

    The strict numeric parser separates absent/malformed fields from nonfinite
    numbers and exponents Decimal cannot represent. finite_scale bounds extreme
    exponents before exact arithmetic and distinguishes measured zero from a
    nonzero result too small for the response's float representation.
    Keep the caller's multiplication/division order for ordinary float rounding.
    This is a read-only conversion: normalized archive rows are never fallbacks.
    """
    numeric_reason = ["numeric_overflow:unit_conversion"]
    missing_reason = ["unsupported_or_missing_value"]
    try:
        source = number(value)
    except InvalidOperation:
        # NaN/Infinity is no measurement; only an exponent Decimal cannot hold overflows.
        loose = loose_number(value)
        nonfinite = loose is not None and not loose.is_finite()
        return None, missing_reason if nonfinite else numeric_reason
    if source is None:
        return None, missing_reason
    result = finite_scale(source, multiplier, divisor)
    return result, numeric_reason if result is None else []


def method_details(payload: dict[str, Any], metadata: object) -> dict[str, Any]:
    source = metadata if isinstance(metadata, dict) else {}
    return {
        "measurementMethod": payload.get("measurementMethod"),
        "measurementLocation": payload.get("measurementLocation"),
        "recordingMethod": source.get("recordingMethod"),
    }


def method_key(payload: dict[str, Any], metadata: object) -> str:
    details = method_details(payload, metadata)
    return payload_hash(list(details.values()))


class BudgetError(Exception):
    pass


@dataclass
class Inputs:
    rows: list[RawHealthData]
    measurements: list[ManualMeasurement]
    workouts: list[ManualWorkout]
    devices: dict[tuple[str, str], str]
    fingerprint: str

    def source(self, kind: str, metadata: object, fallback: str) -> str:
        fingerprint = payload_hash(metadata)
        decision = self.devices.get((kind, fingerprint))
        # Device membership alone does not make recording methods equivalent.
        method = stable_source(metadata) or {}
        recording = method.get("recordingMethod") if isinstance(method, dict) else None
        return (
            "cihaz-" + payload_hash([kind, decision, recording])
            if decision is not None and isinstance(metadata, dict) and metadata
            else fallback
        )


@dataclass(frozen=True, slots=True)
class MeasurementSnapshot:
    measurements: Sequence[dict[str, Any]]
    excluded: Collection[UUID]
    flags: Sequence[tuple[int, str]]


async def load_inputs(
    session: AsyncSession,
    kinds: Collection[str],
    *,
    manual: bool = False,
    workouts: bool = False,
    dates: CivilDateRange,
) -> Inputs:
    left, stop = input_bounds(dates.first, dates.last, dates.timezone)
    window = (
        RawHealthData.data_type.in_(kinds),
        RawHealthData.source_start_time < stop,
        func.coalesce(RawHealthData.source_end_time, RawHealthData.source_start_time) >= left,
    )
    found, eligible, count = await _load_raw_inputs(session, window, kinds)
    raw_values = await _snapshot_fields(found, _RAW_FIELDS)
    keys = await asyncio.to_thread(_raw_keys, raw_values)
    measurement_window = (
        literal(manual),
        ManualMeasurement.measured_at >= left,
        ManualMeasurement.measured_at < stop,
    )
    measurements = await input_rows(
        session,
        select(ManualMeasurement)
        .where(*measurement_window)
        .order_by(ManualMeasurement.measured_at, ManualMeasurement.id)
        .limit(MAX_INPUT_RECORDS + 1)
        .execution_options(populate_existing=True),
    )
    workout_rows = await input_rows(
        session,
        select(ManualWorkout)
        .where(ManualWorkout.start_time < stop, ManualWorkout.end_time > left, literal(workouts))
        .order_by(ManualWorkout.start_time, ManualWorkout.id)
        .limit(MAX_INPUT_RECORDS + 1)
        .execution_options(populate_existing=True),
    )
    if len(measurements) + len(workout_rows) + count > MAX_INPUT_RECORDS:
        raise BudgetError("input_budget_exceeded")
    excluded = set(
        await session.scalars(
            select(ManualMeasurement.id).where(
                *measurement_window, ManualMeasurement.id.in_(excluded_measurements())
            )
        )
    )
    # Hash append-only events as well as effective state: mark then unmark also expires a cursor.
    flags = await measurement_flags(session, measurement_window, MAX_INPUT_RECORDS)
    if len(flags) > MAX_INPUT_RECORDS:
        raise BudgetError("input_budget_exceeded")
    decisions = await source_decisions(session, keys)
    devices = {(r.data_type, r.fingerprint): r.device for r in decisions}
    measurement_values = await _snapshot_fields(measurements, _MEASUREMENT_FIELDS)
    workout_values = await _snapshot_fields(workout_rows, _WORKOUT_FIELDS)
    flag_values = [tuple(row) for row in flags]
    decision_values = await _snapshot_fields(decisions, _DEVICE_FIELDS)
    fingerprint = await asyncio.to_thread(
        _input_fingerprint,
        raw_values,
        eligible,
        workout_values,
        decision_values,
        manual=MeasurementSnapshot(measurement_values, excluded, flag_values),
    )
    return Inputs(
        [r for r in found if r.id in eligible],
        [r for r in measurements if r.id not in excluded],
        workout_rows,
        devices,
        fingerprint,
    )


async def _load_raw_inputs(session: AsyncSession, window: tuple, kinds: Collection[str]):
    """Preflight SQL size/count and load rows without one large ORM hydration stall."""
    count, size = (
        await session.execute(
            select(func.count(), func.sum(func.length(cast(RawHealthData.raw_json, String)))).where(
                *window
            )
        )
    ).one()
    if count > MAX_INPUT_RECORDS or (size or 0) > MAX_INPUT_BYTES:
        raise BudgetError("input_budget_exceeded")
    found = await input_rows(
        session,
        select(RawHealthData)
        .where(*window)
        .order_by(RawHealthData.id)
        .execution_options(populate_existing=True),
    )
    eligible = set(
        await session.scalars(
            select(RawHealthData.id).where(*window, raw_counted(data_types=tuple(kinds)))
        )
    )
    return found, eligible, count


_RAW_FIELDS = (
    "id",
    "data_type",
    "raw_json",
    "source_start_time",
    "source_end_time",
    "source_timezone",
    "source_deleted_at",
    "is_quarantined",
)
_MEASUREMENT_FIELDS = ("id", "metric", "value", "measured_at")
_WORKOUT_FIELDS = (
    "id",
    "start_time",
    "end_time",
    "exercise_type",
    "calories_kcal",
    "distance_meters",
)
_DEVICE_FIELDS = ("data_type", "fingerprint", "device", "id")


async def _snapshot_fields(rows: Sequence, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Copy loaded scalar values on the session's thread, yielding between small batches.

    Workers receive only plain values; no ORM instances, session, lazy attributes or
    database calls cross the boundary. JSON payloads are read-only throughout this load.
    """
    values = []
    for offset in range(0, len(rows), 256):
        values.extend(
            {field: getattr(row, field) for field in fields} for row in rows[offset : offset + 256]
        )
        await asyncio.sleep(0)
    return values


def _raw_keys(rows: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    if sum(len(canonical_json(row["raw_json"]).encode()) for row in rows) > MAX_INPUT_BYTES:
        raise BudgetError("input_budget_exceeded")
    return sorted(
        {(row["data_type"], payload_hash(row["raw_json"].get("dataSource"))) for row in rows}
    )


def _input_fingerprint(
    found: Sequence[dict[str, Any]],
    eligible: Collection[UUID],
    workout_rows: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    *,
    manual: MeasurementSnapshot,
) -> str:
    """Include excluded rows and append-only decisions so round trips expire cursors too."""
    signature = {
        "raw": (
            [
                str(r["id"]),
                r["raw_json"],
                str(r["source_start_time"]),
                str(r["source_end_time"]),
                r["source_timezone"],
                str(r["source_deleted_at"]),
                r["is_quarantined"],
                r["id"] in eligible,
            ]
            for r in found
        ),
        "manual": (
            [
                str(r["id"]),
                r["metric"],
                str(r["value"]),
                str(r["measured_at"]),
                r["id"] in manual.excluded,
            ]
            for r in manual.measurements
        ),
        "workouts": (
            [
                str(r["id"]),
                str(r["start_time"]),
                str(r["end_time"]),
                r["exercise_type"],
                str(r["calories_kcal"]),
                str(r["distance_meters"]),
            ]
            for r in workout_rows
        ),
        "flags": ([str(key), action] for key, action in manual.flags),
        "devices": ([r["data_type"], r["fingerprint"], r["device"], r["id"]] for r in decisions),
    }
    return _signature_hash(signature)


def _signature_hash(signature: dict[str, Iterable[list[Any]]]) -> str:
    """Hash the same canonical JSON one row at a time.

    A single large json.dumps holds the GIL even inside to_thread. Streaming rows
    avoids that pause and the temporary signature lists and giant encoded string.
    """
    digest = sha256(b"{")
    for position, key in enumerate(sorted(signature)):
        if position:
            digest.update(b",")
        digest.update(canonical_json(key).encode() + b":[")
        for offset, row in enumerate(signature[key]):
            if offset:
                digest.update(b",")
            digest.update(canonical_json(row).encode())
        digest.update(b"]")
    digest.update(b"}")
    return digest.hexdigest()


def evidence(
    raw: RawHealthData,
    day: date,
    value: float | None = None,
    field: str | None = None,
    reasons: Sequence[str] = (),
) -> EvidenceData:
    return EvidenceData(
        record=raw.google_data_point_id,
        date=str(day),
        value=value,
        start=as_utc(raw.source_start_time).isoformat() if raw.source_start_time else None,
        end=as_utc(raw.source_end_time).isoformat() if raw.source_end_time else None,
        source_timezone=raw.source_timezone,
        field=field,
        reason_codes=list(reasons),
    )


def fallback_source(raw: RawHealthData) -> str:
    metadata = raw.raw_json.get("dataSource")
    return "kaynak-" + payload_hash(
        metadata
        if isinstance(metadata, dict) and metadata
        else {"record": raw.google_data_point_id}
    )
