"""Five daily adapters, separated by whole source stream and measurement definition."""

from collections import defaultdict
from datetime import date, datetime
from itertools import pairwise
from typing import Any, cast

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.eligibility import raw_counted
from app.models.health import SleepSession, Steps
from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.parsers.common import number, parse_date, payload_for, payload_hash
from app.parsers.google_daily import DAILY_SPO2_FIELDS, daily_spo2
from app.services.readers.dashboard_manual import latest_measurements
from app.services.readers.measurement_windows import (
    interval_window,
    sleep_window,
    step_window,
)
from app.services.shared.finite_math import finite_number, finite_scale, finite_sum

from .canonical_sleep import SleepEvidenceAccumulator
from .canonical_types import CanonicalDay
from .health_windows import bounds as bounds
from .interpretation_data import Group, Groups
from .sleep_estimate import sleep_estimate

# Reject negative daily heart/HRV/oxygen summaries on their source Decimal, and let
# no alias rescue a rejected (negative or nonfinite) field of the same definition.
# Older cached days stay under their original method; rebuilding revalidates the
# raw payload without rewriting archived evidence or previous method results.
METHOD = "source-daily/12"

# Bound decoded raw documents, not the complete observation/timeline output.
_RAW_BATCH = 100
# Bound input rows independently of the required complete manual evidence output.
_MANUAL_BATCH = 1000
DAILY_FIELDS = {
    "daily-resting-heart-rate": ("resting_bpm", "bpm", {"beatsPerMinute": "daily_resting"}),
    "daily-heart-rate-variability": (
        "hrv_ms",
        "ms",
        {
            "averageHeartRateVariabilityMilliseconds": "daily_average_hrv",
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds": "daily_rmssd",
            "rmssd": "daily_rmssd",
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": "deep_sleep_rmssd",
        },
    ),
    "daily-oxygen-saturation": (
        "spo2",
        "%",
        dict.fromkeys(DAILY_SPO2_FIELDS, "daily_average"),
    ),
}


def _base(raw: Row[Any], metric: str, unit: str, definition: str, basis: str) -> Group:
    source = raw.raw_json.get("dataSource")
    known = isinstance(source, dict) and bool(source)
    # An absent source is not evidence that unrelated records share a stream.
    fingerprint = payload_hash(
        source if known else {"unknown_source_record": raw.google_data_point_id}
    )
    return {
        "metric": metric,
        "source": "kaynak-" + fingerprint,
        "source_metadata": source,
        "definition": definition,
        "unit": unit,
        "time_basis": basis,
        "method_version": METHOD,
        "value": None,
        "uncertainties": [] if known else ["source_unknown"],
        "observations": [],
    }


def _observation(raw: Row[Any], value: float | None, **extra: Any) -> dict[str, Any]:
    return {
        "record": raw.google_data_point_id,
        "value": value,
        "source_start": as_utc(raw.source_start_time).isoformat()
        if raw.source_start_time
        else None,
        "source_end": as_utc(raw.source_end_time).isoformat() if raw.source_end_time else None,
        "source_timezone": raw.source_timezone,
        **extra,
    }


def finite(value: object) -> float | None:
    return finite_number(value)


def _summary_value(payload: dict, field: str) -> tuple[float | None, list[str], bool]:
    """Validate each definition independently, preserving rejected field evidence.

    SQL rounding and float underflow must not turn a tiny negative into zero.
    The flag marks a rejected number: negative, NaN, infinite or beyond float
    range. The caller then skips the definition's later aliases in that record,
    as the dashboard and coverage readers select the first numeric field before
    validating it (maintainer, 2026-11-13). A non-numeric field selects nothing.
    """
    supplied = number(payload.get(field))
    if supplied is not None and supplied.is_finite() and supplied < 0:
        return None, [f"negative_measurement:{field}"], True
    value = finite(supplied)
    return value, [], supplied is not None and value is None


async def _summaries(session: AsyncSession, day: date, groups: Groups):
    first, stop = bounds(day, "UTC")  # payload date is a civil date, not a UTC instant
    rows = await session.stream(
        select(
            RawHealthData.data_type,
            RawHealthData.google_data_point_id,
            RawHealthData.raw_json,
            RawHealthData.source_start_time,
            RawHealthData.source_end_time,
            RawHealthData.source_timezone,
        ).where(
            RawHealthData.data_type.in_(DAILY_FIELDS),
            RawHealthData.source_start_time >= first,
            RawHealthData.source_start_time < stop,
            raw_counted(data_types=tuple(DAILY_FIELDS)),
        ),
        execution_options={"yield_per": _RAW_BATCH},
    )
    try:
        async for raw in rows:
            payload = payload_for(RecordType(raw.data_type), raw.raw_json)
            if parse_date(payload.get("date")) != day:
                continue
            metric, unit, fields = DAILY_FIELDS[raw.data_type]
            if metric == "spo2":
                field, _ = daily_spo2(payload)
                # One definition and one selected alias per daily oxygen record.
                fields = {field: fields[field]} if field is not None else {}
            rejected = set()
            for field, definition in fields.items():
                # Keep an explicitly missing definition visible without inventing a zero.
                if field not in payload or definition in rejected:
                    continue
                value, reasons, invalid = _summary_value(payload, field)
                if invalid:
                    rejected.add(definition)
                base = _base(raw, metric, unit, definition, "civil_day_summary")
                metadata = payload.get("dailyRestingHeartRateMetadata")
                if metadata:
                    base["measurement_metadata"] = metadata
                    # Distinct calculation methods must not be silently made equivalent.
                    base["definition"] += ":" + payload_hash(metadata)
                key = (metric, base["source"], base["definition"])
                group = groups.setdefault(key, base)
                group["uncertainties"].extend(reasons)
                group["observations"].append(
                    _observation(
                        raw,
                        value,
                        source_day=day.isoformat(),
                        field=field,
                        **({"assumptions": reasons} if reasons else {}),
                    )
                )
    finally:
        await rows.close()
    for group in groups.values():
        values = {item["value"] for item in group["observations"] if item["value"] is not None}
        if any(item["value"] is None for item in group["observations"]):
            group["uncertainties"].append("missing_observations")
        if len(values) == 1:
            group["value"] = values.pop()
        elif len(values) > 1:
            group["uncertainties"].append("conflicting_values")
        else:
            group["uncertainties"].append("no_value")


async def _interval_records(
    session: AsyncSession,
    model: type[Steps] | type[SleepSession],
    kind: str,
    first: datetime,
    stop: datetime,
):
    raw = aliased(RawHealthData)
    source_candidates = select(RawHealthData.google_data_point_id).where(
        RawHealthData.data_type == kind,
        *interval_window(
            kind, RawHealthData.source_start_time, RawHealthData.source_end_time, first, stop
        ),
    )
    # Sleep belongs only to its current wake day, including raw-only arrivals.
    # Preserve the step candidate union; bound both sides before joining.
    candidates = (
        source_candidates
        if kind == "sleep"
        else select(model.google_data_point_id)
        .where(*interval_window(kind, model.start_time, model.end_time, first, stop))
        .union(source_candidates)
    ).subquery()
    return await session.stream(
        select(
            raw.google_data_point_id,
            raw.raw_json,
            raw.source_start_time,
            raw.source_end_time,
            raw.source_timezone,
        )
        .join(candidates, raw.google_data_point_id == candidates.c.google_data_point_id)
        .where(
            raw.data_type == kind,
            raw_counted(raw, (kind,)),
            # Retained normalized evidence cannot supply missing current dates.
            raw.source_start_time.is_not(None),
            raw.source_end_time.is_not(None),
        ),
        execution_options={"yield_per": _RAW_BATCH},
    )


def _step_observation(
    record: Row[Any],
    first: datetime,
    stop: datetime,
    groups: Groups,
    spans: dict[tuple[str, str, str], list[tuple[datetime, datetime, float | None]]],
):
    allocation = step_window(
        record.raw_json,
        first,
        stop,
        missing_bounds=(as_utc(record.source_start_time), as_utc(record.source_end_time)),
    )
    if allocation is None:
        return
    left, right, count = allocation.left, allocation.right, allocation.count
    definition = "interval_count_prorated"
    base = _base(record, "steps", "count", definition, "interval_allocation")
    key = ("steps", base["source"], definition)
    group = groups.setdefault(key, base)
    if right <= left:
        group["uncertainties"].append("invalid_interval")
        group["observations"].append(
            _observation(record, None, start=left.isoformat(), end=right.isoformat())
        )
    elif count is None:
        group["uncertainties"].extend(("no_value", "missing_observations"))
        group["observations"].append(
            _observation(record, None, start=left.isoformat(), end=right.isoformat())
        )
    else:
        _, seconds, duration = allocation.term
        value = finite_scale(count, seconds, duration)
        if value is None:
            group["uncertainties"].append("numeric_overflow:interval_allocation")
        spans[key].append((allocation.start, allocation.stop, value))
        group["observations"].append(
            _observation(
                record, value, start=left.isoformat(), end=right.isoformat(), source_value=count
            )
        )


def _sleep_observation(
    record: Row[Any],
    first: datetime,
    stop: datetime,
    groups: Groups,
    spans: dict[tuple[str, str, str], SleepEvidenceAccumulator],
):
    measurement = sleep_window(record.raw_json, first, stop, identifier=record.google_data_point_id)
    if measurement is None:
        return
    item, payload = measurement
    definition = "session_asleep_estimate"
    base = _base(record, "sleep_seconds", "s", definition, "wake_day")
    key = ("sleep_seconds", base["source"], definition)
    group = groups.setdefault(key, base)
    spans[key].add(item, payload)
    estimate = sleep_estimate([(item, payload)])
    group["observations"].append(
        _observation(
            record,
            estimate.seconds,
            start=item.start_time.isoformat(),
            end=item.end_time.isoformat(),
            assumptions=estimate.assumptions,
        )
    )


def _finish_sleep(group: Group, values: SleepEvidenceAccumulator):
    estimate, timeline = values.result()
    group.update(value=estimate.seconds, sleep=timeline)
    group["uncertainties"].extend(estimate.assumptions)
    timed = group["sleep"].get("timeline_stage_seconds")
    timed_asleep = (
        sum(timed.get(stage, 0) for stage in ("asleep", "light", "deep", "rem"))
        if timed is not None
        else None
    )
    group["sleep"].update(
        estimated_asleep_seconds=estimate.seconds,
        timed_asleep_seconds=timed_asleep,
        estimate_minus_timed_asleep_seconds=(
            estimate.seconds - timed_asleep
            if estimate.seconds is not None and timed_asleep is not None
            else None
        ),
    )
    if group["sleep"].get("session_overlap_seconds", 0):
        group["uncertainties"].append("overlap_estimated")
    if any(cell["state"] == "conflict" for cell in group["sleep"]["timeline"] or []):
        group["uncertainties"].append("conflicting_stages")


def _finish_steps(group: Group, values: list[tuple[datetime, datetime, float | None]]):
    values.sort(key=lambda row: row[:2])
    if any(b > c for (_, b, _), (c, _, _) in pairwise(values)):
        group["uncertainties"].append("overlapping_intervals")
    elif not any(item["value"] is None for item in group["observations"]):
        group["value"] = finite_sum(cast(list[float], [value for _, _, value in values]))
        if group["value"] is None:
            group["uncertainties"].append("numeric_overflow:daily_total")
    group["uncertainties"].append("uniform_interval_allocation")


async def _intervals(session: AsyncSession, day: date, timezone: str, groups: Groups):
    first, stop = bounds(day, timezone)
    step_spans = defaultdict(list)
    records = await _interval_records(session, Steps, "steps", first, stop)
    try:
        async for record in records:
            _step_observation(record, first, stop, groups, step_spans)
    finally:
        await records.close()
    for key, values in step_spans.items():
        _finish_steps(groups[key], values)

    sleep_spans = defaultdict(SleepEvidenceAccumulator)
    records = await _interval_records(session, SleepSession, "sleep", first, stop)
    try:
        async for record in records:
            _sleep_observation(record, first, stop, groups, sleep_spans)
    finally:
        await records.close()
    for key, accumulator in sleep_spans.items():
        _finish_sleep(groups[key], accumulator)


async def _manual_observations(session: AsyncSession, metric: str, stop: datetime):
    """Retain all latest evidence, with only a fixed batch of projected input rows.

    UUID order agrees with the canonical string order. Replace entry-time ordering
    in SQL so the output needs neither an ORM input list nor a second sorted list.
    Eligibility and latest-time selection stay with the shared measurement query.
    """
    statement = (
        latest_measurements(metric, stop)
        .with_only_columns(
            ManualMeasurement.id, ManualMeasurement.value, ManualMeasurement.measured_at
        )
        .order_by(None)
        .order_by(ManualMeasurement.id)
    )
    rows = await session.stream(statement, execution_options={"yield_per": _MANUAL_BATCH})
    observations = []
    try:
        async for row in rows:
            observations.append(
                {
                    "record": str(row.id),
                    "value": float(row.value),
                    "measured_at": as_utc(row.measured_at).isoformat(),
                }
            )
    finally:
        await rows.close()
    return observations


async def _manual_profile(session: AsyncSession, stop: datetime):
    result = []
    for metric in ("weight", "height", "body_fat", "muscle"):
        observations = await _manual_observations(session, metric, stop)
        if observations:
            # Two distinct float values prove a conflict; retain no growing value set.
            values = set()
            for item in observations:
                values.add(item["value"])
                if len(values) > 1:
                    break
            result.append(
                {
                    "metric": metric,
                    "source": "manuel",
                    "unit": {"weight": "kg", "height": "cm", "body_fat": "%", "muscle": "%"}[
                        metric
                    ],
                    "method_version": METHOD,
                    "time_basis": "latest_before_day_stop",
                    "observations": observations,
                    "value": next(iter(values)) if len(values) == 1 else None,
                    "uncertainties": ["conflicting_values"] if len(values) > 1 else [],
                }
            )
    return result


async def canonical_day(session: AsyncSession, day: date, timezone: str) -> dict:
    groups = {}
    await _summaries(session, day, groups)
    await _intervals(session, day, timezone, groups)
    for group in groups.values():
        group["observations"].sort(
            key=lambda item: (item["source_start"] or "", item["record"], item.get("field", ""))
        )
        if group["metric"] == "sleep_seconds":
            totals_by_span = defaultdict(set)
            for item in group["observations"]:
                if item["value"] is not None:
                    totals_by_span[(item["start"], item["end"])].add(item["value"])
            if any(len(values) > 1 for values in totals_by_span.values()):
                group["uncertainties"].append("conflicting_session_totals")
        group["uncertainties"] = sorted(set(group["uncertainties"]))
        codes = group["uncertainties"]
        group["state"] = (
            "conflict"
            if any(
                code.startswith("conflicting_") or code == "overlapping_intervals" for code in codes
            )
            else "no_value"
            if group["value"] is None
            else "estimated"
            if group["time_basis"] != "civil_day_summary"
            else "measured"
        )
    first, stop = bounds(day, timezone)
    result = {
        "date": day.isoformat(),
        "timezone": timezone,
        "method_version": METHOD,
        "day_seconds": (stop - first).total_seconds(),
        "streams": [groups[key] for key in sorted(groups)],
        "manual_profile": await _manual_profile(session, stop),
        "missing_metrics": sorted(
            {"steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2"}
            - {group["metric"] for group in groups.values() if group["value"] is not None}
        ),
        "source_selection": "none",
        "intraday_samples": "not_used_as_daily_summaries",
    }

    return CanonicalDay.model_validate(result).model_dump(mode="json", exclude_unset=True)
