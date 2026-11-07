"""Normalize Apple Health records into the shared normalized tables, tagged ``APPLE``.

Input is ``AppleRecord`` values (app/apple/records.py): straight from an import or a
receiver request (``normalize_records``) or read back from the raw archive, newest version
per ``(channel, kind, key)`` (``normalize_archive``). Output rows carry ``provider =
'APPLE'``, the record key ``apple:<channel>:<kind>:<key>[:<part>]`` in
``google_data_point_id`` (app/core/providers.py) and nothing Google-shaped (no
``raw_health_data`` row, no sighting), so the Google readers never see them; the Apple
readers select ``provider = 'APPLE'`` (app/services/apple_readers.py).

Columns every Apple row shares:

* ``source_record_id``: ``<channel>:<type>:<key>[:<part>]``: the channel (``export`` or
  ``hae``), the record type as received (``HKQuantityTypeIdentifierStepCount``,
  ``step_count``, ``HKWorkoutActivityTypeRunning``, ...), the raw record's key, and a
  part when the record yields several rows (a blood-pressure point's ``systolic`` and
  ``diastolic``, an activity summary's attribute) or, for ``sleep_sessions``, the sleep
  stage of the row: ``core``, ``deep``, ``rem``, ``awake``, ``asleep`` (asleep, stage
  unknown), ``in_bed`` or ``night`` (a Health Auto Export summarized night). The stage is
  always the last ``:``-separated field of a sleep row's ``source_record_id``
  (``sleep_stage``).
* ``device``: the Health source (``sourceName``; Health Auto Export's ``source``), else the
  device model. The readers deduplicate per source (a Watch and an iPhone both count steps),
  and two sources can share a model (the iPhone itself and a pedometer app on it), so the
  source is the identity kept here; the model is in ``apple_sources`` (Cihazlarım).
* ``source_timezone``: the UTC offset the record's start was written with (``+01:00``).

What becomes what (export ``HKQuantityTypeIdentifier…``/``HKCategoryTypeIdentifier…`` names
without the prefix, and Health Auto Export metric names):

* ``StepCount``/``step_count`` -> ``steps``;
* ``DistanceWalkingRunning``, ``DistanceCycling``, ``DistanceSwimming``,
  ``DistanceWheelchair``, ``DistanceDownhillSnowSports`` and their ``*_distance`` names ->
  ``distance`` (meters from m, km, mi, ft, yd, cm);
* ``ActiveEnergyBurned``/``active_energy`` and ``BasalEnergyBurned``/``basal_energy_burned``
  -> ``energy_expenditure`` (kcal from kcal, Cal, kJ, J, cal); active and basal stay apart
  by the type in ``source_record_id``;
* ``HeartRate``/``heart_rate`` -> ``heart_rate`` (Health Auto Export's ``Avg``, else
  ``qty``; ``Min``/``Max`` stay in the raw archive);
* ``RestingHeartRate``/``resting_heart_rate`` -> ``resting_heart_rate``;
* ``HeartRateVariabilitySDNN``/``heart_rate_variability`` -> ``heart_rate_variability``
  with ``rmssd = NULL`` and ``metrics = {"sdnn_ms": x}``: Apple measures SDNN, never RMSSD;
* ``OxygenSaturation``/``blood_oxygen_saturation`` -> ``oxygen_saturation`` in percent (a
  fraction of at most 1, export.xml's form, is multiplied by 100);
* ``RespiratoryRate``/``respiratory_rate`` -> ``respiratory_rate``;
* ``VO2Max``/``vo2_max`` -> ``vo2_max``;
* ``BodyMass``/``weight_body_mass`` -> ``weight`` (kg from kg, lb, g, oz, st);
* ``BodyTemperature``, ``AppleSleepingWristTemperature`` and their names -> ``temperature``
  (degC from degC, degF), ``metric_type`` the record type;
* ``SleepAnalysis``/``sleep_analysis`` -> ``sleep_sessions``: export category records and
  unsummarized Health Auto Export points give one row per segment (``duration_seconds`` the
  segment; Core -> ``light_sleep_seconds``, Deep -> ``deep_sleep_seconds``, REM ->
  ``rem_sleep_seconds``, Awake -> ``awake_seconds``, Asleep/Unspecified -> duration only,
  InBed -> duration only, stage ``in_bed``); a summarized Health Auto Export point gives one
  row per night (``sleepStart``..``sleepEnd``, else ``inBedStart``..``inBedEnd``;
  ``duration_seconds`` = ``totalSleep``, the stage hours as seconds);
* export ``Workout`` and Health Auto Export workouts (v1 and v2) -> ``exercise_sessions``
  (``exercise_type`` the activity type without ``HKWorkoutActivityType``, or the workout's
  ``name``; duration in seconds; calories and distance from the attributes, else the
  ``WorkoutStatistics``, else Health Auto Export's ``activeEnergyBurned``/``activeEnergy``
  and ``distance`` objects, converted);
* every other quantity record and Health Auto Export metric -> ``scalar_metrics`` with
  ``data_type = "apple:<type>"`` and its unit. A Health Auto Export point without ``qty``
  (or ``Avg``) gives one row per numeric field, ``data_type = "apple:<name>:<field>"``
  (``blood_pressure`` -> ``…:systolic`` and ``…:diastolic``). An ``ActivitySummary`` gives
  one row per numeric attribute (``apple:ActivitySummary:<attribute>``) over its calendar
  day (UTC midnight to midnight, like Garmin's day rows).
* Category records other than sleep (stand hours, mindful sessions, events) ->
  ``scalar_metrics`` too, ``value`` NULL (or the number when the category value is one) and
  the category value text in ``attributes["value"]``: nothing is silently dropped.

Skipped and counted (``NormalizeStats.skipped``): a record without a usable time, a value a
required column cannot hold, an export ``Correlation`` (its child records are records of
their own and are normalized as such) and any other record without a number or category
value. The raw archive keeps all of them.

Idempotent by comparison (like app/services/garmin_normalize.py, with its own writer that
only ever writes onto ``provider = 'APPLE'`` rows): existing rows are read first, an
unchanged row is not written, a changed one is updated in place, a new one inserted.
Running it twice writes nothing the second time.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, DecimalException
from typing import Any, cast

from sqlalchemy import Table, and_, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, aliased

from app.apple.records import (
    ACTIVITY_SUMMARY,
    CATEGORY,
    HAE_METRIC,
    HAE_WORKOUT,
    QUANTITY,
    RECORD,
    WORKOUT,
    AppleRecord,
    parse_apple_date,
)
from app.core.logging import event
from app.core.providers import APPLE, apple_record_key
from app.core.timeutil import as_utc
from app.models.apple import AppleRawRecord
from app.models.health import (
    Distance,
    EnergyExpenditure,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RespiratoryRate,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
    Temperature,
    Vo2Max,
    Weight,
)
from app.parsers import numeric
from app.parsers.text import prepare_optional_text
from app.services.garmin_normalize import NormalizeStats

logger = logging.getLogger(__name__)

Model = type[DeclarativeBase]
Row = tuple[Model, dict[str, Any]]

HK_QUANTITY = "HKQuantityTypeIdentifier"
HK_CATEGORY = "HKCategoryTypeIdentifier"
HK_WORKOUT = "HKWorkoutActivityType"
SCALAR_PREFIX = "apple:"

# Target of each record type (export identifiers without the prefix, and HAE names).
STEPS, DISTANCE, ENERGY, HEART, RESTING, HRV, SPO2 = (
    "steps",
    "distance",
    "energy",
    "heart",
    "resting",
    "hrv",
    "spo2",
)
RESPIRATORY, VO2, WEIGHT, TEMPERATURE, SLEEP = (
    "respiratory",
    "vo2",
    "weight",
    "temperature",
    "sleep",
)
TARGETS: dict[str, str] = {
    "StepCount": STEPS,
    "step_count": STEPS,
    "DistanceWalkingRunning": DISTANCE,
    "DistanceCycling": DISTANCE,
    "DistanceSwimming": DISTANCE,
    "DistanceWheelchair": DISTANCE,
    "DistanceDownhillSnowSports": DISTANCE,
    "walking_running_distance": DISTANCE,
    "cycling_distance": DISTANCE,
    "swimming_distance": DISTANCE,
    "wheelchair_distance": DISTANCE,
    "distance_wheelchair": DISTANCE,
    "downhill_snow_sports_distance": DISTANCE,
    "ActiveEnergyBurned": ENERGY,
    "BasalEnergyBurned": ENERGY,
    "active_energy": ENERGY,
    "basal_energy_burned": ENERGY,
    "HeartRate": HEART,
    "heart_rate": HEART,
    "RestingHeartRate": RESTING,
    "resting_heart_rate": RESTING,
    "HeartRateVariabilitySDNN": HRV,
    "heart_rate_variability": HRV,
    "OxygenSaturation": SPO2,
    "blood_oxygen_saturation": SPO2,
    "RespiratoryRate": RESPIRATORY,
    "respiratory_rate": RESPIRATORY,
    "VO2Max": VO2,
    "vo2_max": VO2,
    "BodyMass": WEIGHT,
    "weight_body_mass": WEIGHT,
    "BodyTemperature": TEMPERATURE,
    "AppleSleepingWristTemperature": TEMPERATURE,
    "body_temperature": TEMPERATURE,
    "apple_sleeping_wrist_temperature": TEMPERATURE,
    "SleepAnalysis": SLEEP,
    "sleep_analysis": SLEEP,
}

# Unit factors to the column's unit (keys compared as given first, then case-folded).
METERS = {"m": 1, "km": 1000, "mi": "1609.344", "ft": "0.3048", "yd": "0.9144", "cm": "0.01"}
KCAL = {"kcal": 1, "Cal": 1, "kJ": "0.2390057361376673", "J": "0.0002390057361376673"}
KCAL_EXACT = {"cal": "0.001"}  # a small calorie: only with this exact spelling
KILOGRAMS = {"kg": 1, "lb": "0.45359237", "lbs": "0.45359237", "g": "0.001"}
KILOGRAMS |= {"oz": "0.028349523125", "st": "6.35029318"}
SECONDS = {"s": 1, "sec": 1, "min": 60, "hr": 3600, "h": 3600, "hour": 3600, "hours": 3600}
SECONDS |= {"ms": "0.001", "d": 86400, "day": 86400}

# Sleep stage of a record value (export category values and HAE ``value`` texts).
SLEEP_STAGES = {
    "HKCategoryValueSleepAnalysisAsleepCore": "core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem",
    "HKCategoryValueSleepAnalysisAwake": "awake",
    "HKCategoryValueSleepAnalysisAsleep": "asleep",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "asleep",
    "HKCategoryValueSleepAnalysisInBed": "in_bed",
    "core": "core",
    "deep": "deep",
    "rem": "rem",
    "awake": "awake",
    "asleep": "asleep",
    "unspecified": "asleep",
    "asleep unspecified": "asleep",
    "in bed": "in_bed",
    "inbed": "in_bed",
}
ASLEEP_STAGES = ("core", "deep", "rem", "asleep")
NIGHT = "night"
STAGE_COLUMN = {
    "core": "light_sleep_seconds",
    "deep": "deep_sleep_seconds",
    "rem": "rem_sleep_seconds",
    "awake": "awake_seconds",
}

# Fields of a HAE metric point that are not values.
_POINT_TIME_FIELDS = {
    "date",
    "start",
    "end",
    "startDate",
    "endDate",
    "source",
    "sleepStart",
    "sleepEnd",
    "inBedStart",
    "inBedEnd",
}
_ACTIVITY_UNITS = {
    "appleMoveTime": "min",
    "appleMoveTimeGoal": "min",
    "appleExerciseTime": "min",
    "appleExerciseTimeGoal": "min",
    "appleStandHours": "count",
    "appleStandHoursGoal": "count",
}
# Columns compared to decide whether an existing row changed (never the bookkeeping ones).
_SKIP_COMPARE = {"id", "created_at", "updated_at", "is_quarantined", "quarantine_reason"}
_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})\s*$")


# -- small helpers ---------------------------------------------------------------------------


def short_type(record_type: str) -> str:
    """The record type without the HealthKit prefix (``StepCount``, ``SleepAnalysis``)."""
    for prefix in (HK_QUANTITY, HK_CATEGORY, HK_WORKOUT):
        if record_type.startswith(prefix):
            return record_type[len(prefix) :]
    return record_type


def target_of(record_type: str) -> str | None:
    return TARGETS.get(short_type(record_type))


def sleep_stage(source_record_id: str | None) -> str | None:
    """The stage of an Apple ``sleep_sessions`` row (the last field of its id)."""
    if not source_record_id:
        return None
    return source_record_id.rsplit(":", 1)[-1]


def channel_of(source_record_id: str | None) -> str | None:
    """The channel (``export`` or ``hae``) of an Apple row's ``source_record_id``."""
    if not source_record_id:
        return None
    return source_record_id.split(":", 1)[0]


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return numeric.number(value)
    except DecimalException:
        return None


def _factor(table: dict[str, Any], unit: object, exact: dict[str, Any] | None = None):
    if not isinstance(unit, str):
        return None
    text = unit.strip()
    if exact and text in exact:
        return Decimal(str(exact[text]))
    if text in table:
        return Decimal(str(table[text]))
    folded = {name.casefold(): value for name, value in table.items()}
    found = folded.get(text.casefold())
    return Decimal(str(found)) if found is not None else None


def convert(value: Decimal | None, unit: object, table: dict[str, Any], *, default=None):
    """``value`` in the table's unit; ``default`` names the unit assumed when none is given
    (an unknown unit gives None: a number of unknown scale is not guessed)."""
    if value is None:
        return None
    if unit is None or (isinstance(unit, str) and not unit.strip()):
        if default is None:
            return None
        unit = default
    factor = _factor(table, unit, KCAL_EXACT if table is KCAL else None)
    return value * factor if factor is not None else None


def celsius(value: Decimal | None, unit: object) -> Decimal | None:
    if value is None:
        return None
    text = unit.strip().casefold() if isinstance(unit, str) else "degc"
    if text in {"degc", "°c", "c", "celsius", ""}:
        return value
    if text in {"degf", "°f", "f", "fahrenheit"}:
        return (value - 32) * 5 / 9
    return None


def percent(value: Decimal | None) -> Decimal | None:
    """SpO2 as a percent: export.xml stores a fraction (0.97), HAE a percent (97)."""
    if value is None:
        return None
    return value * 100 if value <= 1 else value


def offset_of(text: object) -> str | None:
    """``+01:00`` from an Apple timestamp's offset (``2026-11-06 07:00:00 +0100``)."""
    if not isinstance(text, str):
        return None
    match = _OFFSET.search(text)
    if match is None:
        return None
    return f"{match.group(1)}{match.group(2)}:{match.group(3)}"


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


def _hae_bucket(aggregation: object) -> timedelta | None:
    """The span of a summarized HAE point from the automation's aggregation header
    (undocumented values: anything naming seconds/minutes/hours/days/weeks)."""
    if not isinstance(aggregation, str):
        return None
    text = aggregation.casefold()
    for word, span in (
        ("sec", timedelta(seconds=1)),
        ("min", timedelta(minutes=1)),
        ("hour", timedelta(hours=1)),
        ("day", timedelta(days=1)),
        ("week", timedelta(weeks=1)),
    ):
        if word in text:
            return span
    return None


@dataclass(frozen=True)
class _Context:
    """What every row of one record shares."""

    record: AppleRecord

    def common(self, *parts: str, detail: str | None = None) -> dict[str, Any]:
        record = self.record
        suffix = [*parts] + ([detail] if detail else [])
        return {
            "google_data_point_id": apple_record_key(
                record.channel, record.kind, record.key, *parts
            ),
            "provider": APPLE,
            "source_record_id": ":".join((record.channel, record.type, record.key, *suffix)),
            "device": record.source_name or record.device,
            "source_timezone": self.timezone,
        }

    @property
    def timezone(self) -> str | None:
        body = self.record.body
        if self.record.kind == HAE_METRIC:
            point = body.get("point")
            point = point if isinstance(point, dict) else {}
            names = ("start", "startDate", "sleepStart", "inBedStart", "date")
            return next((offset_of(point.get(name)) for name in names if point.get(name)), None)
        if self.record.kind == HAE_WORKOUT:
            workout = body.get("workout")
            return offset_of(workout.get("start")) if isinstance(workout, dict) else None
        return offset_of(body.get("startDate"))


# -- rows of one record ------------------------------------------------------------------------


def _point(record: AppleRecord) -> dict[str, Any]:
    point = record.body.get("point")
    return point if isinstance(point, dict) else {}


def _interval(record: AppleRecord) -> tuple[datetime | None, datetime | None]:
    """The record's start and end; a HAE point without an end spans its aggregation bucket
    (or is an instant when the bucket is unknown)."""
    start, end = record.start, record.end
    if record.kind == HAE_METRIC:
        point = _point(record)
        start = start or parse_apple_date(point.get("start") or point.get("date"))
        end = end or parse_apple_date(point.get("end"))
        if start is not None and end is None:
            bucket = _hae_bucket(record.body.get("aggregation"))
            end = start + bucket if bucket is not None else start
    elif record.kind in (QUANTITY, CATEGORY, RECORD):
        start = start or parse_apple_date(record.body.get("startDate"))
        end = end or parse_apple_date(record.body.get("endDate"))
    if start is not None and end is None:
        end = start
    return start, end


def _value_and_unit(record: AppleRecord) -> tuple[Decimal | None, str | None]:
    """The record's number and unit (export ``value``/``unit``; HAE ``Avg`` or ``qty`` and
    the metric's ``units``)."""
    if record.kind == HAE_METRIC:
        point = _point(record)
        value = _number(point.get("Avg"))
        if value is None:
            value = _number(point.get("qty"))
        units = record.body.get("units")
        return value, units if isinstance(units, str) else None
    unit = record.body.get("unit")
    return _number(record.body.get("value")), unit if isinstance(unit, str) else None


def _sleep_rows(record: AppleRecord, context: _Context) -> list[Row]:
    point = _point(record) if record.kind == HAE_METRIC else {}
    if record.kind == HAE_METRIC and any(
        name in point for name in ("totalSleep", "sleepStart", "inBedStart")
    ):
        return _summarized_night(record, point, context)
    raw = point.get("value") if record.kind == HAE_METRIC else record.body.get("value")
    stage = SLEEP_STAGES.get(raw) if isinstance(raw, str) else None
    if stage is None and isinstance(raw, str):
        stage = SLEEP_STAGES.get(raw.strip().casefold())
    start, end = _interval(record)
    if stage is None or start is None:
        return []
    if end is None or end <= start:
        hours = _number(point.get("qty")) if record.kind == HAE_METRIC else None
        seconds = convert(hours, record.body.get("units"), SECONDS, default="hr")
        if seconds is None or seconds <= 0:
            return []
        end = start + timedelta(seconds=float(seconds))
    seconds = Decimal(str((end - start).total_seconds()))
    values: dict[str, Any] = {
        **context.common(detail=stage),
        "start_time": start,
        "end_time": end,
        "duration_seconds": seconds,
        "deep_sleep_seconds": None,
        "light_sleep_seconds": None,
        "rem_sleep_seconds": None,
        "awake_seconds": None,
        "efficiency": None,
    }
    if stage in STAGE_COLUMN:
        values[STAGE_COLUMN[stage]] = seconds
    return [(SleepSession, values)]


def _summarized_night(record: AppleRecord, point: dict[str, Any], context: _Context) -> list[Row]:
    start = parse_apple_date(point.get("sleepStart")) or parse_apple_date(point.get("inBedStart"))
    end = parse_apple_date(point.get("sleepEnd")) or parse_apple_date(point.get("inBedEnd"))
    if start is None or end is None or end <= start:
        return []
    units = record.body.get("units")

    def seconds(name: str) -> Decimal | None:
        return convert(_number(point.get(name)), units, SECONDS, default="hr")

    total = seconds("totalSleep")
    if total is None:
        parts = [seconds(name) for name in ("asleep", "core", "deep", "rem")]
        known = [part for part in parts if part is not None]
        total = sum(known, Decimal(0)) if known else None
    return [
        (
            SleepSession,
            {
                **context.common(detail=NIGHT),
                "start_time": start,
                "end_time": end,
                "duration_seconds": total,
                "deep_sleep_seconds": seconds("deep"),
                "light_sleep_seconds": seconds("core"),
                "rem_sleep_seconds": seconds("rem"),
                "awake_seconds": seconds("awake"),
                "efficiency": None,
            },
        )
    ]


def _measurement_rows(record: AppleRecord, target: str, context: _Context) -> list[Row]:
    value, unit = _value_and_unit(record)
    start, end = _interval(record)
    if value is None or start is None or end is None:
        return []
    common = context.common()
    if target == STEPS:
        return [(Steps, {**common, "start_time": start, "end_time": end, "count": value})]
    if target == DISTANCE:
        meters = convert(value, unit, METERS, default="m")
        if meters is None:
            return []
        return [(Distance, {**common, "start_time": start, "end_time": end, "meters": meters})]
    if target == ENERGY:
        kcal = convert(value, unit, KCAL, default="kcal")
        if kcal is None:
            return []
        return [(EnergyExpenditure, {**common, "start_time": start, "end_time": end, "kcal": kcal})]
    if target == HEART:
        return [(HeartRate, {**common, "timestamp": start, "bpm": value, "motion_context": None})]
    if target == RESTING:
        return [
            (
                RestingHeartRate,
                {**common, "timestamp": start, "bpm": value, "calculation_method": None},
            )
        ]
    if target == HRV:
        return [
            (
                HeartRateVariability,
                {
                    **common,
                    "timestamp": start,
                    "rmssd": None,  # Apple measures SDNN; it is never stored as RMSSD
                    "metrics": {"sdnn_ms": float(value)},
                },
            )
        ]
    if target == SPO2:
        return [(OxygenSaturation, {**common, "timestamp": start, "spo2": percent(value)})]
    if target == RESPIRATORY:
        return [
            (
                RespiratoryRate,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": record.type,
                    "breaths_per_minute": value,
                },
            )
        ]
    if target == VO2:
        return [
            (
                Vo2Max,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": record.type,
                    "ml_per_kg_min": value,
                    "fitness_level": None,
                },
            )
        ]
    if target == WEIGHT:
        kilograms = convert(value, unit, KILOGRAMS, default="kg")
        if kilograms is None:
            return []
        return [(Weight, {**common, "timestamp": start, "kilograms": kilograms})]
    if target == TEMPERATURE:
        degrees = celsius(value, unit)
        if degrees is None:
            return []
        return [
            (
                Temperature,
                {
                    **common,
                    "timestamp": start,
                    "metric_type": record.type,
                    "celsius": degrees,
                    "baseline_celsius": None,
                },
            )
        ]
    return []


def _scalar(
    context: _Context,
    data_type: str,
    start: datetime,
    end: datetime | None,
    value: Decimal | None,
    unit: object,
    attributes: dict[str, Any],
    *parts: str,
) -> Row:
    return (
        ScalarMetric,
        {
            **context.common(*parts),
            "data_type": (SCALAR_PREFIX + data_type)[:128],
            "start_time": start,
            "end_time": end,
            "value": value,
            "unit": unit[:64] if isinstance(unit, str) and unit else None,
            "attributes": attributes,
        },
    )


def _plain(values: dict[str, Any], skip: Iterable[str] = ()) -> dict[str, Any]:
    """The text and number fields of a body (no nested values), for ``attributes``."""
    left = set(skip)
    return {
        name: value
        for name, value in values.items()
        if name not in left and isinstance(value, str | int | float) and not isinstance(value, bool)
    }


def _generic_rows(record: AppleRecord, context: _Context) -> list[Row]:
    """``scalar_metrics`` rows of a record no dedicated table takes."""
    start, end = _interval(record)
    if record.kind == ACTIVITY_SUMMARY:
        return _activity_summary_rows(record, context)
    if start is None:
        return []
    if record.kind == HAE_METRIC:
        point = _point(record)
        units = record.body.get("units")
        value = _number(point.get("qty"))
        if value is None:
            value = _number(point.get("Avg"))
        if value is not None:
            attributes = _plain(point, {"qty", *_POINT_TIME_FIELDS})
            return [_scalar(context, record.type, start, end, value, units, attributes)]
        rows = []
        for name in sorted(point):
            if name in _POINT_TIME_FIELDS:
                continue
            number = _number(point[name])
            if number is None:
                continue
            rows.append(
                _scalar(context, f"{record.type}:{name}", start, end, number, units, {}, name)
            )
        return rows
    body = record.body
    metadata = body.get("metadata")
    attributes: dict[str, Any] = {}
    if isinstance(metadata, dict) and metadata:
        attributes["metadata"] = metadata
    raw = body.get("value")
    value = _number(raw)
    if record.kind == CATEGORY or (value is None and isinstance(raw, str) and raw.strip()):
        if not isinstance(raw, str) or not raw.strip():
            return []
        attributes["value"] = raw
        return [_scalar(context, record.type, start, end, value, body.get("unit"), attributes)]
    if value is None:
        return []
    return [_scalar(context, record.type, start, end, value, body.get("unit"), attributes)]


def _activity_summary_rows(record: AppleRecord, context: _Context) -> list[Row]:
    body = record.body
    raw_day = body.get("dateComponents")
    try:
        day = date.fromisoformat(raw_day[:10]) if isinstance(raw_day, str) else None
    except ValueError:
        day = None
    if day is None:
        return []
    start, end = _midnight(day), _midnight(day + timedelta(days=1))
    rows = []
    for name in sorted(body):
        if name == "dateComponents" or name.endswith("Unit"):
            continue
        value = _number(body[name])
        if value is None:
            continue
        unit = (
            body.get("activeEnergyBurnedUnit")
            if name.startswith("activeEnergyBurned")
            else _ACTIVITY_UNITS.get(name)
        )
        rows.append(_scalar(context, f"{record.type}:{name}", start, end, value, unit, {}, name))
    return rows


def _first_stat(statistics: object, wanted: Iterable[str]) -> tuple[Decimal | None, object]:
    names = tuple(wanted)
    for item in statistics if isinstance(statistics, list) else ():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if isinstance(kind, str) and short_type(kind) in names:
            value = _number(item.get("sum"))
            if value is not None:
                return value, item.get("unit")
    return None, None


def _quantity(value: object) -> tuple[Decimal | None, object]:
    """A HAE ``{qty, units}`` object, or a list of them (summed when the units agree)."""
    if isinstance(value, dict):
        return _number(value.get("qty")), value.get("units")
    if isinstance(value, list):
        total, units = Decimal(0), None
        found = False
        for item in value:
            if not isinstance(item, dict):
                continue
            number = _number(item.get("qty"))
            if number is None:
                continue
            if units is not None and item.get("units") != units:
                return None, None
            units = item.get("units")
            total += number
            found = True
        return (total, units) if found else (None, None)
    return _number(value), None


def _workout_row(record: AppleRecord, context: _Context) -> list[Row]:
    if record.kind == WORKOUT:
        body = record.body
        start = record.start or parse_apple_date(body.get("startDate"))
        end = record.end or parse_apple_date(body.get("endDate"))
        kind = short_type(body.get("workoutActivityType") or record.type)
        duration = convert(
            _number(body.get("duration")), body.get("durationUnit"), SECONDS, default="min"
        )
        calories = convert(
            _number(body.get("totalEnergyBurned")),
            body.get("totalEnergyBurnedUnit"),
            KCAL,
            default="kcal",
        )
        if calories is None:
            sum_, unit = _first_stat(body.get("statistics"), ("ActiveEnergyBurned",))
            calories = convert(sum_, unit, KCAL, default="kcal")
        meters = convert(
            _number(body.get("totalDistance")), body.get("totalDistanceUnit"), METERS, default="km"
        )
        if meters is None:
            distances = (name for name, target in TARGETS.items() if target == DISTANCE)
            sum_, unit = _first_stat(body.get("statistics"), distances)
            meters = convert(sum_, unit, METERS, default="km")
    else:
        workout = record.body.get("workout")
        workout = workout if isinstance(workout, dict) else {}
        start = record.start or parse_apple_date(workout.get("start"))
        end = record.end or parse_apple_date(workout.get("end"))
        name = workout.get("name")
        kind = name.strip() if isinstance(name, str) and name.strip() else record.type
        duration = _number(workout.get("duration"))
        energy = workout.get("activeEnergyBurned")
        if energy is None:
            energy = workout.get("activeEnergy")
        sum_, unit = _quantity(energy)
        calories = convert(sum_, unit, KCAL, default="kcal")
        sum_, unit = _quantity(workout.get("distance"))
        meters = convert(sum_, unit, METERS, default="km")
    if start is None:
        return []
    if end is None or end <= start:
        if duration is None or duration <= 0:
            return []
        end = start + timedelta(seconds=float(duration))
    if duration is None:
        duration = Decimal(str((end - start).total_seconds()))
    return [
        (
            ExerciseSession,
            {
                **context.common(),
                "start_time": start,
                "end_time": end,
                "exercise_type": kind[:128] if kind else None,
                "duration_seconds": duration,
                "calories_kcal": calories,
                "distance_meters": meters,
            },
        )
    ]


def rows_for_record(record: AppleRecord) -> list[Row]:
    """The normalized rows of one record (empty when it has none it can give)."""
    context = _Context(record)
    if record.kind in (WORKOUT, HAE_WORKOUT):
        return _workout_row(record, context)
    if record.kind == ACTIVITY_SUMMARY:
        return _activity_summary_rows(record, context)
    target = target_of(record.type)
    if target == SLEEP:
        return _sleep_rows(record, context)
    if target is not None and record.kind in (QUANTITY, HAE_METRIC):
        rows = _measurement_rows(record, target, context)
        if rows:
            return rows
    if record.kind == RECORD and isinstance(record.body.get("records"), list):
        return []  # a Correlation: its child records are normalized on their own
    return _generic_rows(record, context)


def _prepared(model: Model, values: dict[str, Any]) -> dict[str, Any] | None:
    row = dict(values)
    try:
        numeric.prepare_numeric(model, row)
    except DecimalException:
        return None
    prepare_optional_text(model, row)
    return row


def build_rows(records: Iterable[AppleRecord]) -> tuple[dict[Model, list[dict[str, Any]]], int]:
    """All rows of the records per model, and how many records gave no row (or a row a
    required column could not hold)."""
    rows: dict[Model, list[dict[str, Any]]] = {}
    skipped = 0
    for record in records:
        produced = rows_for_record(record)
        if not produced:
            skipped += 1
            continue
        for model, values in produced:
            prepared = _prepared(model, values)
            if prepared is None:
                skipped += 1
                continue
            rows.setdefault(model, []).append(prepared)
    return rows, skipped


# -- writing -------------------------------------------------------------------------------------


def _comparable(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, Decimal):
        return value.normalize()
    return value


async def write_rows(
    session: AsyncSession, model: Model, rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Insert the new rows, update the changed ones, leave the rest alone; returns (inserted,
    updated, unchanged). Matched by ``google_data_point_id``; only ``provider = 'APPLE'``
    rows are ever written onto."""
    from app.services.storage import dialect_insert

    if not rows:
        return 0, 0, 0
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[row["google_data_point_id"]] = row  # the last (newest) version wins
    rows = [unique[key] for key in sorted(unique)]
    table = cast(Table, model.__table__)
    columns = [column.name for column in table.columns if column.name not in _SKIP_COMPARE]
    existing: dict[str, dict[str, Any]] = {}
    keys = sorted(unique)
    for offset in range(0, len(keys), 1000):
        found = await session.execute(
            select(*(table.c[name] for name in ("id", *columns))).where(
                table.c.google_data_point_id.in_(keys[offset : offset + 1000])
            )
        )
        for row in found.mappings():
            existing[row["google_data_point_id"]] = dict(row)
    inserts: list[dict[str, Any]] = []
    updated = unchanged = 0
    now = datetime.now(UTC)
    for row in rows:
        current = existing.get(row["google_data_point_id"])
        if current is None:
            inserts.append(row)
            continue
        if current["provider"] != APPLE:
            unchanged += 1
            continue
        changed = {
            name: value
            for name, value in row.items()
            if name in current and _comparable(current[name]) != _comparable(value)
        }
        if not changed:
            unchanged += 1
            continue
        await session.execute(
            update(table)
            .where(table.c.id == current["id"])
            .values(**changed, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        updated += 1
    if inserts:
        conflict = ["google_data_point_id"]
        if "data_type" in table.c:
            conflict.insert(0, "data_type")
        statement = dialect_insert(session, table).on_conflict_do_nothing(index_elements=conflict)
        for offset in range(0, len(inserts), 1000):
            await session.execute(statement, inserts[offset : offset + 1000])
    return len(inserts), updated, unchanged


def _add(total: NormalizeStats, part: NormalizeStats) -> None:
    total.inserted += part.inserted
    total.updated += part.updated
    total.unchanged += part.unchanged
    total.skipped += part.skipped
    for table, counts in part.tables.items():
        current = total.tables.setdefault(table, {"inserted": 0, "updated": 0, "unchanged": 0})
        for name, count in counts.items():
            current[name] = current.get(name, 0) + count


async def normalize_records(
    session: AsyncSession, records: Sequence[AppleRecord]
) -> NormalizeStats:
    """Normalize the given records (several versions of one key: the last one wins) into the
    shared tables; idempotent (see the module docstring). The caller commits."""
    latest: dict[tuple[str, str, str], AppleRecord] = {}
    for record in records:
        latest[(record.channel, record.kind, record.key)] = record
    rows, skipped = build_rows(latest.values())
    stats = NormalizeStats(skipped=skipped)
    for model in sorted(rows, key=lambda item: item.__tablename__):
        inserted, updated, unchanged = await write_rows(session, model, rows[model])
        stats.note(model.__tablename__, inserted, updated, unchanged)
    return stats


async def normalize_archive(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    channel: str | None = None,
    batch: int = 5000,
) -> NormalizeStats:
    """Normalize the raw archive again: the newest version of every ``(channel, kind, key)``
    (a record with no newer row of its key), optionally only records starting in ``[since,
    until)`` (a record without a start time is then left out) and of one channel. Reads in
    pages of ``batch`` rows by id, never the whole archive at once; idempotent."""
    newer = aliased(AppleRawRecord)
    newest = ~exists(
        select(newer.id).where(
            and_(
                newer.channel == AppleRawRecord.channel,
                newer.kind == AppleRawRecord.kind,
                newer.key == AppleRawRecord.key,
                newer.id > AppleRawRecord.id,
            )
        )
    )
    filters = [newest]
    if since is not None:
        filters.append(AppleRawRecord.start_time >= since)
    if until is not None:
        filters.append(AppleRawRecord.start_time < until)
    if channel is not None:
        filters.append(AppleRawRecord.channel == channel)
    columns = (
        AppleRawRecord.id,
        AppleRawRecord.channel,
        AppleRawRecord.kind,
        AppleRawRecord.type,
        AppleRawRecord.key,
        AppleRawRecord.body,
        AppleRawRecord.body_sha256,
        AppleRawRecord.source_name,
        AppleRawRecord.device,
        AppleRawRecord.start_time,
        AppleRawRecord.end_time,
    )
    from app.services.apple_archive import record_from_row

    total = NormalizeStats()
    last_id = 0
    pages = 0
    while True:
        found = (
            await session.execute(
                select(*columns)
                .where(*filters, AppleRawRecord.id > last_id)
                .order_by(AppleRawRecord.id)
                .limit(batch)
            )
        ).all()
        if not found:
            break
        last_id = found[-1].id
        records = [
            # A transient row (never added to the session): the identity map stays empty.
            record_from_row(
                AppleRawRecord(
                    channel=row.channel,
                    kind=row.kind,
                    type=row.type,
                    key=row.key,
                    body=row.body,
                    body_sha256=row.body_sha256,
                    source_name=row.source_name,
                    device=row.device,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
            )
            for row in found
        ]
        _add(total, await normalize_records(session, records))
        pages += 1
        if len(found) < batch:
            break
    event(
        logger,
        "APPLE_NORMALIZED",
        channel=channel,
        pages=pages,
        inserted=total.inserted,
        updated=total.updated,
        unchanged=total.unchanged,
        skipped=total.skipped,
    )
    return total
