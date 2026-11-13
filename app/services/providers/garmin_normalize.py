"""Normalize the Garmin raw archive into the shared normalized tables, tagged ``GARMIN``.

Reads the last observed ``garmin_raw_records`` version per kind and key over a calendar-day window
and writes the shared normalized tables (``steps``, ``heart_rate``, ``sleep_sessions``, …) with
``provider = 'GARMIN'``, ``source_record_id = '<kind>:<key>'`` and the record key
``garmin:<kind>:<key>[:<part>]`` in ``google_data_point_id`` (app/core/providers.py). Nothing
Google-shaped is written: no ``raw_health_data`` row, no sighting, no identity. Google's
readers join a normalized row to its raw row by key and so never see a Garmin row; the Garmin
readers select ``provider = 'GARMIN'`` (app/services/providers/garmin_readers.py).

Idempotent by design, not by overwrite: existing rows are read first, a row whose values are
unchanged is not written (the derived-day triggers of ``steps``/``sleep_sessions`` therefore
fire only for a new or changed row), a changed row is updated in place and a new one
inserted. Running it twice over the same archive writes nothing the second time.
Version 2 keys end in ``:v2``; earlier normalized rows stay untouched and readers prefer v2.
Daily heart samples also commit an append-only membership set in this transaction; samples
no longer produced remain stored but are excluded from current reads (not quarantined).
Daily projections also append their prepared key or an explicit invalid result, with
selected raw input ids. Values and validity become visible in the same transaction.
Activities likewise append completed conversion results, retaining invalidated old rows.
What becomes what (field names as the Personal API returns them, docs/reference/garmin.md):

* ``steps`` (``totalSteps``, ``totalDistance``) -> ``steps`` and ``distance``, over the civil
  day's UTC bounds (``startTimestampGMT``/``endTimestampGMT`` of the day's ``heart-rate``
  record; the nearest heart-rate day's UTC offset when there is none);
* ``heart-rate`` (``heartRateValues [[ms, bpm], …]``) -> ``heart_rate``, one row per sample;
  ``restingHeartRate`` (or ``daily-summaries`` ``heartRate.restingValue``, preferred) ->
  ``resting_heart_rate`` at the civil day's UTC midnight, like Google's daily summaries;
* ``sleep`` (``sleepStartTimestampGMT``/``sleepEndTimestampGMT``, ``sleepTimeSeconds``, the
  stage seconds) -> ``sleep_sessions``; the ``sleep-summaries`` record stands in when the day
  kind is missing;
* ``hrv-summaries``/``hrv`` (``lastNightAvg``, ``status``, ``lastNight5MinHigh``) ->
  ``heart_rate_variability`` (``rmssd`` = last night's average, the rest in ``metrics``);
* ``spo2`` (``averageSpO2``) -> ``oxygen_saturation``;
* ``stress`` (``values.overallStressLevel``) and ``daily-summaries`` ``bodyBattery`` ->
  ``scalar_metrics`` as ``garmin-stress`` and ``garmin-body-battery`` (no dedicated table);
* ``activities`` (``startTimeGMT``, ``duration``, ``activityType.typeKey``, ``calories``,
  ``distance``) -> ``exercise_sessions``.

A record without the fields a row needs is skipped and counted; a value a column cannot hold
is left NULL when optional and skips the row when required (``prepare_numeric``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core import normalization as numeric
from app.core.logging import event
from app.core.normalization import instant_ms as _instant_ms
from app.core.normalization import parse_datetime as _instant_text
from app.core.providers import GARMIN, garmin_record_key
from app.core.timeutil import as_utc
from app.models.health import (
    Distance,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
)
from app.parsers.text import prepare_optional_text

from .garmin_activity_identity import activity_key
from .garmin_activity_state import inherit_activity_quarantine, write_activity_results
from .garmin_current import newest_records as newest_records
from .garmin_daily_state import write_daily_projections
from .garmin_membership import write_heart_sets
from .garmin_normalize_types import NormalizeStats as NormalizeStats
from .garmin_normalize_writing import unique_heart_rows, write_rows
from .garmin_time import (
    adjacent_hearts,
    day_zones,
    heart_zone,
    nearest_zone,
    zone_label,
)
from .garmin_time import (
    day_bounds as day_bounds,
)
from .garmin_versions import VERSION

logger = logging.getLogger(__name__)

# The raw kinds a normalization window reads (the others, e.g. body-battery reports, stay raw).
KINDS = (
    "steps",
    "heart-rate",
    "daily-summaries",
    "sleep",
    "sleep-summaries",
    "hrv-summaries",
    "hrv",
    "spo2",
    "stress",
    "activities",
)
STRESS_TYPE = "garmin-stress"
BODY_BATTERY_TYPE = "garmin-body-battery"


# -- field helpers -------------------------------------------------------------------------


type NormalizedRows = list[tuple[type[DeclarativeBase], dict[str, Any]]]


@dataclass(frozen=True)
class _ScalarWindow:
    start: datetime
    end: datetime


def _get(record: Any, *path: str) -> Any:
    value = record
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def _number(value: Any) -> Decimal | None:
    try:
        return numeric.number(value)
    except DecimalException:
        return None


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


def _day(key: str) -> date | None:
    try:
        return date.fromisoformat(key[:10])
    except ValueError:
        return None


def _common(kind: str, key: str, device: str, *parts: str) -> dict[str, Any]:
    return {
        "google_data_point_id": garmin_record_key(kind, key, *parts, VERSION),
        "provider": GARMIN,
        "source_record_id": f"{kind}:{key}",
        "device": device,
        "source_timezone": None,
    }


# -- rows ----------------------------------------------------------------------------------


def rows_for_day(
    day: date, records: dict[str, Any], device: str, zone: timezone | None = None
) -> NormalizedRows:
    """The normalized rows of one calendar day from its newest records per kind."""
    key = day.isoformat()
    found: NormalizedRows = []
    steps, heart, summary = (
        records.get("steps"),
        records.get("heart-rate"),
        records.get("daily-summaries"),
    )
    zone = zone or heart_zone(day, heart)
    start, end = day_bounds(day, heart, zone or UTC)
    count = _number(_get(steps, "totalSteps"))
    if count is not None:
        found.append(
            (
                Steps,
                {
                    **_common("steps", key, device),
                    "start_time": start,
                    "end_time": end,
                    "count": count,
                },
            )
        )
    meters = _number(_get(steps, "totalDistance"))
    if meters is not None:
        found.append(
            (
                Distance,
                {
                    **_common("distance", key, device),
                    "start_time": start,
                    "end_time": end,
                    "meters": meters,
                },
            )
        )
    _heart_rows(day, key, heart, summary, device, found)
    _sleep_rows(day, key, records, device, found)
    _scalar_rows(key, records, summary, device, _ScalarWindow(start, end), found)
    for _, values in found:
        values["source_timezone"] = zone_label(day, zone) if zone is not None else None
    return found


def _heart_rows(
    day: date,
    key: str,
    heart: object,
    summary: object,
    device: str,
    found: NormalizedRows,
):
    """Append valid samples and the independent daily resting heart rate.

    Only a JSON list is a sample collection. A malformed collection yields no
    samples; it must still allow the resting value and other daily rows through.
    Membership publication distinguishes this archived empty result from an
    absent raw record in the caller's normalization transaction.
    """
    samples = _get(heart, "heartRateValues")
    if not isinstance(samples, list):
        samples = []
    for sample in samples:
        if not isinstance(sample, list) or len(sample) < 2:
            continue
        moment, bpm = _instant_ms(sample[0]), _number(sample[1])
        if moment is None or bpm is None or not isinstance(sample[0], int):
            continue
        found.append(
            (
                HeartRate,
                {
                    **_common("heart-rate", key, device, str(sample[0])),
                    "timestamp": moment,
                    "bpm": bpm,
                    "motion_context": None,
                },
            )
        )
    resting = _number(_get(summary, "heartRate", "restingValue"))
    if resting is None:
        resting = _number(_get(heart, "restingHeartRate"))
    if resting is not None:
        found.append(
            (
                RestingHeartRate,
                {
                    **_common("resting-heart-rate", key, device),
                    "timestamp": _midnight(day),
                    "bpm": resting,
                    "calculation_method": None,
                },
            )
        )


def _sleep_rows(
    day: date,
    key: str,
    records: dict[str, Any],
    device: str,
    found: NormalizedRows,
):
    sleep = records.get("sleep") or records.get("sleep-summaries")
    sleep_start = _instant_ms(_get(sleep, "sleepStartTimestampGMT"))
    sleep_end = _instant_ms(_get(sleep, "sleepEndTimestampGMT"))
    if sleep_start is not None and sleep_end is not None and sleep_end > sleep_start:
        found.append(
            (
                SleepSession,
                {
                    **_common("sleep", key, device),
                    "start_time": sleep_start,
                    "end_time": sleep_end,
                    "duration_seconds": _number(_get(sleep, "sleepTimeSeconds")),
                    "deep_sleep_seconds": _number(_get(sleep, "deepSleepSeconds")),
                    "light_sleep_seconds": _number(_get(sleep, "lightSleepSeconds")),
                    "rem_sleep_seconds": _number(_get(sleep, "remSleepSeconds")),
                    "awake_seconds": _number(_get(sleep, "awakeSleepSeconds")),
                    "efficiency": None,
                },
            )
        )
    hrv = records.get("hrv-summaries") or _get(records.get("hrv"), "hrvSummary")
    average = _number(_get(hrv, "lastNightAvg"))
    if average is not None:
        metrics = {
            name: _get(hrv, name)
            for name in ("status", "lastNight5MinHigh", "weeklyAvg", "baseline")
            if isinstance(_get(hrv, name), str | int | float)
            and not isinstance(_get(hrv, name), bool)
        }
        found.append(
            (
                HeartRateVariability,
                {
                    **_common("hrv", key, device),
                    "timestamp": _midnight(day),
                    "rmssd": average,
                    "metrics": metrics,
                },
            )
        )
    spo2 = _number(_get(records.get("spo2"), "averageSpO2"))
    if spo2 is not None:
        found.append(
            (
                OxygenSaturation,
                {**_common("spo2", key, device), "timestamp": _midnight(day), "spo2": spo2},
            )
        )


def _scalar_rows(
    key: str,
    records: dict[str, Any],
    summary: object,
    device: str,
    window: _ScalarWindow,
    found: NormalizedRows,
):
    stress = _number(_get(records.get("stress"), "values", "overallStressLevel"))
    if stress is None:
        stress = _number(_get(summary, "stress", "avgLevel"))
    if stress is not None:
        durations = _get(records.get("stress"), "values")
        attributes = (
            {name: value for name, value in durations.items() if isinstance(value, int | float)}
            if isinstance(durations, dict)
            else {}
        )
        found.append(
            (
                ScalarMetric,
                {
                    **_common("stress", key, device),
                    "data_type": STRESS_TYPE,
                    "start_time": window.start,
                    "end_time": window.end,
                    "value": stress,
                    "unit": "level",
                    "attributes": attributes,
                },
            )
        )
    battery = _get(summary, "bodyBattery")
    if isinstance(battery, dict):
        levels = {
            name: battery.get(f"{name}Value")
            for name in ("latest", "max", "min", "charged", "drained")
            if isinstance(battery.get(f"{name}Value"), int | float)
        }
        latest = _number(levels.get("latest", levels.get("max")))
        if latest is not None:
            found.append(
                (
                    ScalarMetric,
                    {
                        **_common("body-battery", key, device),
                        "data_type": BODY_BATTERY_TYPE,
                        "start_time": window.start,
                        "end_time": window.end,
                        "value": latest,
                        "unit": "level",
                        "attributes": levels,
                    },
                )
            )


def _activity_end(
    start: datetime, end: datetime | None, duration: Decimal | None
) -> datetime | None:
    """Prefer a valid explicit end, otherwise derive a representable datetime.

    Duration is optional when an end exists; its SQL bounds are checked by _prepared.
    Without an end, only Python's timedelta/datetime range limits the derivation.
    """
    if end is not None and end > start:
        return end
    if duration is None or duration <= 0:
        return None
    try:
        return start + timedelta(seconds=float(duration))
    except OverflowError:
        # A finite Decimal can become float infinity, exceed timedelta's range,
        # or push an otherwise valid start beyond datetime.max.
        return None


def activity_row(record: Any, device: str) -> dict[str, Any] | None:
    """An ``exercise_sessions`` row, or None without an id or representable time bounds."""
    identifier = activity_key(record)
    start = _instant_text(_get(record, "startTimeGMT"))
    duration = _number(_get(record, "duration"))
    if identifier is None or start is None:
        return None
    end = _activity_end(start, _instant_text(_get(record, "endTimeGMT")), duration)
    if end is None:
        return None
    kind = _get(record, "activityType", "typeKey")
    return {
        **_common("activities", identifier, device),
        "start_time": start,
        "end_time": end,
        "exercise_type": kind if isinstance(kind, str) and kind else None,
        "duration_seconds": duration,
        "calories_kcal": _number(_get(record, "calories")),
        "distance_meters": _number(_get(record, "distance")),
    }


def _prepared(model: type[DeclarativeBase], values: dict[str, Any]) -> dict[str, Any] | None:
    """Fit the row to its columns (``prepare_numeric``, ``prepare_optional_text``); None when
    a required number cannot be stored."""
    row = dict(values)
    try:
        numeric.prepare_numeric(model, row)
    except DecimalException:
        return None
    prepare_optional_text(model, row)
    return row


def build_rows(
    latest: dict[str, dict[str, Any]],
    start: date,
    end: date,
    device: str,
    *,
    activity_results: dict[str, str | None] | None = None,
) -> tuple[dict[type[DeclarativeBase], list[dict[str, Any]]], int]:
    """Rows and skipped count; optionally collect activity results by selected raw key.

    Invalid conversions yield NULL; valid activities outside the window yield no result.
    """
    rows: dict[type[DeclarativeBase], list[dict[str, Any]]] = {}
    skipped = 0
    zones = day_zones(latest.get("heart-rate", {}))
    day = start
    while day <= end:
        key = day.isoformat()
        records = {kind: bodies.get(key) for kind, bodies in latest.items() if kind != "activities"}
        for model, values in rows_for_day(day, records, device, nearest_zone(day, zones)):
            prepared = _prepared(model, values)
            if prepared is None:
                skipped += 1
                continue
            rows.setdefault(model, []).append(prepared)
        day += timedelta(days=1)
    if HeartRate in rows:
        rows[HeartRate] = unique_heart_rows(rows[HeartRate])
    activities, activity_skipped = build_activity_rows(
        latest.get("activities", {}), start, end, device, activity_results=activity_results
    )
    if activities:
        rows[ExerciseSession] = activities
    return rows, skipped + activity_skipped


def build_activity_rows(
    bodies: dict[str, Any],
    start: date,
    end: date,
    device: str,
    *,
    activity_results: dict[str, str | None] | None = None,
    restrict_window: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Prepare only selected activities, without walking the daily calendar.

    Invalid conversions yield NULL; valid activities outside the window yield no result.
    Both direct normalization and activity pages use this same preparation and counting.
    Periodic pages finish selected archive work regardless of the moving daily window.
    """
    rows: list[dict[str, Any]] = []
    skipped = 0
    for key, body in bodies.items():
        values = activity_row(body, device)
        prepared = _prepared(ExerciseSession, values) if values is not None else None
        if prepared is None:
            skipped += 1
            if activity_results is not None:
                activity_results[key] = None
            continue
        activity_start = as_utc(prepared["start_time"]).date()
        # Activities keyed by id: only those that begin in the window (give or take a day for
        # the civil/UTC offset) belong to it.
        if not restrict_window or start - timedelta(days=1) <= activity_start <= end + timedelta(
            days=1
        ):
            rows.append(prepared)
            if activity_results is not None:
                activity_results[key] = prepared["google_data_point_id"]
    return rows, skipped


async def normalize_window(
    session: AsyncSession,
    start: date,
    end: date,
    *,
    device: str = "Garmin",
    seen_since: datetime | None = None,
    activities: bool = True,
) -> NormalizeStats:
    """Normalize every Garmin record of ``start``..``end`` (calendar days, inclusive) into the
    shared normalized tables; idempotent (see the module docstring). ``seen_since`` limits
    the activities read to keys observed since then (legacy keys use first-seen time);
    ``activities=False`` leaves them to garmin_bounded.py, which pages them."""
    kinds = KINDS if activities else tuple(kind for kind in KINDS if kind != "activities")
    record_ids: dict[tuple[str, str], int] = {}
    latest = await newest_records(
        session, kinds, start, end, seen_since=seen_since, record_ids=record_ids
    )
    if any(kind != "activities" for kind, _ in record_ids):
        latest["heart-rate"] = {
            **await adjacent_hearts(session, start, end),
            **latest["heart-rate"],
        }
    activity_results: dict[str, str | None] = {}
    rows, skipped = build_rows(latest, start, end, device, activity_results=activity_results)
    await inherit_activity_quarantine(
        session, rows.get(ExerciseSession, []), latest.get("activities", {})
    )
    stats = NormalizeStats(skipped=skipped)
    for model in sorted(rows, key=lambda item: item.__tablename__):
        model_rows = sorted(rows[model], key=lambda row: row["google_data_point_id"])
        inserted, updated, unchanged = await write_rows(session, model, model_rows)
        stats.note(model.__tablename__, inserted, updated, unchanged)
    await write_heart_sets(session, record_ids, rows.get(HeartRate, []))
    await write_daily_projections(session, record_ids, rows)
    await write_activity_results(session, record_ids, activity_results)
    event(
        logger,
        "GARMIN_NORMALIZED",
        start=start.isoformat(),
        end=end.isoformat(),
        inserted=stats.inserted,
        updated=stats.updated,
        unchanged=stats.unchanged,
        skipped=stats.skipped,
    )
    return stats
