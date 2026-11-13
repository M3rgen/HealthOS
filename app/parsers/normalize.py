from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, DecimalException, localcontext
from typing import Any

from app.core import normalization as numeric
from app.core.records import RecordType
from app.parsers.common import (
    device_name,
    payload_for,
    source_id,
    temporal,
)
from app.parsers.sleep import sleep_period_seconds, sleep_stages


@dataclass(frozen=True)
class NormalizedRecord:
    kind: str
    values: dict[str, Any]


def _heart_rate_motion_context(payload: dict) -> str | None:
    """Read optional context without rejecting an otherwise usable measurement.

    Nonempty metadata objects retain their original precedence. Empty or malformed
    metadata may fall back to the alternate field; an invalid context inside the
    selected object is absent, rather than being coerced into SQL text.
    """
    for key in ("metadata", "heartRateMetadata"):
        metadata = payload.get(key)
        if not isinstance(metadata, dict) or not metadata:
            continue
        context = metadata.get("motionContext")
        # Google supplies a string enum. Other JSON types are not context labels,
        # even if a database driver happens to accept a scalar as text.
        if isinstance(context, str):
            return context
        return None
    return None


def normalize(spec: RecordType, record: object) -> NormalizedRecord | None:
    # Only parser/arithmetic failures become skipped normalization. Database writes and
    # connection failures remain outside this boundary and still roll back the page.
    try:
        with localcontext(Context()):
            return _normalize(spec, record)
    except DecimalException:
        return None


def _normalize(spec: RecordType, record: object) -> NormalizedRecord | None:
    if not isinstance(record, dict):
        return None
    payload = payload_for(spec, record)
    if not payload:
        return None
    start, end, timezone = temporal(spec, record)
    common = {
        "google_data_point_id": source_id(spec, record),
        "device": device_name(record),
        "source_timezone": timezone,
    }
    if start:
        sample = _SAMPLE_NORMALIZERS.get(spec.name)
        if sample is not None:
            return sample(spec.name, payload, common, start)
        interval = _INTERVAL_NORMALIZERS.get(spec.name)
        if interval is not None and end:
            return interval(payload, common, start, end)
        # Missing interval ends have always fallen back to a scalar measurement.
        return _scalar(spec, payload, common, start, end)
    return None


def _heart_rate(kind: str, payload: dict, common: dict, start: datetime) -> NormalizedRecord | None:
    bpm = numeric.first_number(payload, ("beatsPerMinute", "bpm", "value"))
    return (
        NormalizedRecord(
            "HeartRate",
            {
                **common,
                "timestamp": start,
                "bpm": bpm,
                "motion_context": _heart_rate_motion_context(payload),
            },
        )
        if bpm is not None
        else None
    )


def _heart_rate_variability(
    kind: str, payload: dict, common: dict, start: datetime
) -> NormalizedRecord | None:
    rmssd = numeric.first_number(
        payload,
        (
            "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
            "averageHeartRateVariabilityMilliseconds",
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
            "rmssd",
        ),
    )
    return NormalizedRecord(
        "HeartRateVariability",
        {**common, "timestamp": start, "rmssd": rmssd, "metrics": payload},
    )


def _oxygen_saturation(
    kind: str, payload: dict, common: dict, start: datetime
) -> NormalizedRecord | None:
    spo2 = numeric.first_number(payload, ("percentage", "spo2", "averagePercentage", "average"))
    return (
        NormalizedRecord("OxygenSaturation", {**common, "timestamp": start, "spo2": spo2})
        if spo2 is not None
        else None
    )


def _steps(payload: dict, common: dict, start: datetime, end: datetime) -> NormalizedRecord | None:
    count = numeric.first_number(payload, ("count", "countSum", "steps"))
    return (
        NormalizedRecord("Steps", {**common, "start_time": start, "end_time": end, "count": count})
        if count is not None
        else None
    )


def _sleep(payload: dict, common: dict, start: datetime, end: datetime) -> NormalizedRecord | None:
    stages = sleep_stages(payload, strict=True)
    return NormalizedRecord(
        "SleepSession",
        {
            **common,
            "start_time": start,
            "end_time": end,
            "duration_seconds": sleep_period_seconds(payload, start, end)[0],
            "deep_sleep_seconds": stages.get("deep"),
            "light_sleep_seconds": stages.get("light"),
            "rem_sleep_seconds": stages.get("rem"),
            # A measured zero is kept: only a missing stage is unknown.
            "awake_seconds": stages["awake"] if "awake" in stages else stages.get("wake"),
            "efficiency": _sleep_efficiency(payload),
        },
    )


def _exercise(
    payload: dict, common: dict, start: datetime, end: datetime
) -> NormalizedRecord | None:
    metrics = payload.get("metricsSummary", {})
    if not isinstance(metrics, dict):
        metrics = {}
    distance_mm = numeric.first_number(metrics, ("distanceMillimeters",))
    duration = numeric.duration_seconds(payload.get("activeDuration"))
    if duration is None:
        duration = numeric.duration_seconds(payload.get("duration"))
    return NormalizedRecord(
        "ExerciseSession",
        {
            **common,
            "start_time": start,
            "end_time": end,
            "exercise_type": payload.get("exerciseType") or payload.get("displayName"),
            "duration_seconds": duration
            if duration is not None
            else Decimal(str((end - start).total_seconds())),
            "calories_kcal": numeric.first_number(metrics, ("caloriesKcal",)),
            "distance_meters": numeric.from_milli(distance_mm) if distance_mm is not None else None,
        },
    )


def _resting_heart_rate(
    kind: str, payload: dict, common: dict, start: datetime
) -> NormalizedRecord | None:
    bpm = numeric.number(payload.get("beatsPerMinute"))
    metadata = payload.get("dailyRestingHeartRateMetadata", {})
    return (
        NormalizedRecord(
            "RestingHeartRate",
            {
                **common,
                "timestamp": start,
                "bpm": bpm,
                "calculation_method": metadata.get("calculationMethod")
                if isinstance(metadata, dict)
                else None,
            },
        )
        if bpm is not None
        else None
    )


def _vo2_max(kind: str, payload: dict, common: dict, start: datetime) -> NormalizedRecord | None:
    value = numeric.number(payload.get("runVo2Max" if kind == "run-vo2-max" else "vo2Max"))
    return (
        NormalizedRecord(
            "Vo2Max",
            {
                **common,
                "timestamp": start,
                "metric_type": kind,
                "ml_per_kg_min": value,
                "fitness_level": payload.get("cardioFitnessLevel"),
            },
        )
        if value is not None
        else None
    )


def _temperature(
    kind: str, payload: dict, common: dict, start: datetime
) -> NormalizedRecord | None:
    value = numeric.first_number(payload, ("temperatureCelsius", "nightlyTemperatureCelsius"))
    return (
        NormalizedRecord(
            "Temperature",
            {
                **common,
                "timestamp": start,
                "metric_type": kind,
                "celsius": value,
                "baseline_celsius": numeric.number(payload.get("baselineTemperatureCelsius")),
            },
        )
        if value is not None
        else None
    )


def _respiratory_rate(
    kind: str, payload: dict, common: dict, start: datetime
) -> NormalizedRecord | None:
    value = numeric.number(payload.get("breathsPerMinute"))
    full_sleep = payload.get("fullSleepStats", {})
    if value is None and isinstance(full_sleep, dict):
        value = numeric.number(full_sleep.get("breathsPerMinute"))
    return (
        NormalizedRecord(
            "RespiratoryRate",
            {
                **common,
                "timestamp": start,
                "metric_type": kind,
                "breaths_per_minute": value,
            },
        )
        if value is not None
        else None
    )


def _active_zone_minutes(
    payload: dict, common: dict, start: datetime, end: datetime
) -> NormalizedRecord | None:
    minutes = numeric.number(payload.get("activeZoneMinutes"))
    return (
        NormalizedRecord(
            "ActiveZoneMinute",
            {
                **common,
                "start_time": start,
                "end_time": end,
                "minutes": minutes,
                "heart_rate_zone": payload.get("heartRateZone"),
            },
        )
        if minutes is not None
        else None
    )


def _distance(
    payload: dict, common: dict, start: datetime, end: datetime
) -> NormalizedRecord | None:
    millimeters = numeric.number(payload.get("millimeters"))
    return (
        NormalizedRecord(
            "Distance",
            {
                **common,
                "start_time": start,
                "end_time": end,
                "meters": numeric.from_milli(millimeters),
            },
        )
        if millimeters is not None
        else None
    )


def _energy(payload: dict, common: dict, start: datetime, end: datetime) -> NormalizedRecord | None:
    kcal = numeric.first_number(payload, ("kcal", "kcalSum"))
    return (
        NormalizedRecord(
            "EnergyExpenditure",
            {**common, "start_time": start, "end_time": end, "kcal": kcal},
        )
        if kcal is not None
        else None
    )


def _weight(kind: str, payload: dict, common: dict, start: datetime) -> NormalizedRecord | None:
    grams = numeric.number(payload.get("weightGrams"))
    return (
        NormalizedRecord(
            "Weight",
            {**common, "timestamp": start, "kilograms": numeric.from_milli(grams)},
        )
        if grams is not None
        else None
    )


def _scalar(
    spec: RecordType, payload: dict, common: dict, start: datetime, end: datetime | None
) -> NormalizedRecord:
    value, unit = _generic_value(payload)
    return NormalizedRecord(
        "ScalarMetric",
        {
            **common,
            "data_type": spec.name,
            "start_time": start,
            "end_time": end,
            "value": value,
            "unit": unit,
            "attributes": payload,
        },
    )


def _sleep_efficiency(payload: dict) -> Decimal | None:
    summary = payload.get("summary", {})
    if not isinstance(summary, dict):
        return None
    asleep = numeric.number(summary.get("minutesAsleep"))
    period = numeric.number(summary.get("minutesInSleepPeriod"))
    if asleep is None or period is None or period == 0:
        return None
    try:
        return (asleep / period) * 100
    except DecimalException:
        # An optional derived ratio must not reject the session it describes.
        return None


def _generic_value(payload: dict) -> tuple[Decimal | None, str | None]:
    ignored = {"year", "month", "day", "hours", "minutes", "seconds", "nanos"}
    candidates = (
        "value",
        "average",
        "count",
        "percentage",
        "kilograms",
        "meters",
        "litersPerMinute",
        "calories",
        "minutes",
    )
    value = numeric.first_number(payload, candidates)
    if value is None:
        for key, candidate in payload.items():
            if key not in ignored and not isinstance(candidate, (dict, list)):
                value = numeric.number(candidate)
                if value is not None:
                    break
    return value, payload.get("unit")


_SAMPLE_NORMALIZERS: dict[str, Callable[[str, dict, dict, datetime], NormalizedRecord | None]] = {
    "heart-rate": _heart_rate,
    "heart-rate-variability": _heart_rate_variability,
    "daily-heart-rate-variability": _heart_rate_variability,
    "oxygen-saturation": _oxygen_saturation,
    "daily-oxygen-saturation": _oxygen_saturation,
    "daily-resting-heart-rate": _resting_heart_rate,
    "vo2-max": _vo2_max,
    "daily-vo2-max": _vo2_max,
    "run-vo2-max": _vo2_max,
    "core-body-temperature": _temperature,
    "daily-sleep-temperature-derivations": _temperature,
    "daily-respiratory-rate": _respiratory_rate,
    "respiratory-rate-sleep-summary": _respiratory_rate,
    "weight": _weight,
}
_INTERVAL_NORMALIZERS: dict[
    str, Callable[[dict, dict, datetime, datetime], NormalizedRecord | None]
] = {
    "steps": _steps,
    "sleep": _sleep,
    "exercise": _exercise,
    "active-zone-minutes": _active_zone_minutes,
    "distance": _distance,
    "active-energy-burned": _energy,
    "total-calories": _energy,
}
