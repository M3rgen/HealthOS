"""Project the raw daily API in SQL, independently of published normalization inputs."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, String, case, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.garmin import GarminRawRecord

from .garmin_current import current_record_ids
from .garmin_steps import negative_steps

_SLEEP = (
    ("sleepTimeSeconds",),
    ("deepSleepSeconds",),
    ("lightSleepSeconds",),
    ("remSleepSeconds",),
    ("awakeSleepSeconds",),
    ("sleepScores", "overall", "value"),
)
FIELDS = {
    "steps": (("totalSteps",), ("totalDistance",)),
    "daily-summaries": (
        ("heartRate", "restingValue"),
        ("stress", "avgLevel"),
        *(
            ("bodyBattery", name + "Value")
            for name in ("max", "min", "latest", "charged", "drained")
        ),
    ),
    "heart-rate": (("restingHeartRate",),),
    "sleep": _SLEEP,
    "sleep-summaries": _SLEEP,
    "hrv-summaries": (("lastNightAvg",),),
    "hrv": (("hrvSummary", "lastNightAvg"),),
    "spo2": (("averageSpO2",),),
    "stress": (("values", "overallStressLevel"),),
}
PATHS = tuple(dict.fromkeys(path for paths in FIELDS.values() for path in paths))
_NUMERIC_STARTS = tuple("-0123456789")


def summary_query(first: date, last: date):
    """One row per current kind/key, only numeric leaves and whole-record truthiness.

    JSON text keeps integer/float tokens distinct, including large integers. A JSON
    number starts with a digit or minus on both supported SQL implementations;
    strings, booleans and even malformed array/object leaves never cross into Python.
    The only root scalar returned is a number used for Python's record truthiness.
    """
    raw = GarminRawRecord
    text = sql_cast(raw.body, String)
    present = case(
        (raw.body.in_([JSON.NULL, {}, [], "", False]), "false"),
        (func.substr(text, 1, 1).in_(_NUMERIC_STARTS), text),
        else_="true",
    )
    columns = []
    for path in PATHS:
        member = raw.body
        for name in path:
            member = member.op("->")(name)
        text = sql_cast(member, String)
        kinds = [kind for kind, paths in FIELDS.items() if path in paths]
        columns.append(
            case((raw.kind.in_(kinds) & func.substr(text, 1, 1).in_(_NUMERIC_STARTS), text))
        )
    ids = current_record_ids(
        raw.kind.in_(FIELDS), raw.key >= first.isoformat(), raw.key <= last.isoformat()
    )
    return (
        select(raw.key, raw.kind, present, *columns)
        .where(raw.id.in_(ids))
        .order_by(raw.key, raw.kind)
    )


def _record(kind: str, present: str, values: tuple) -> dict[str, Any]:
    # Keep nonempty primaries nonempty even when all of their summary fields are absent.
    # That preserves sleep/HRV whole-record fallback without fetching their metadata.
    if not json.loads(present):
        return {}
    record: dict[str, Any] = {}
    for path, value in zip(PATHS, values, strict=True):
        if path not in FIELDS[kind]:
            continue
        target = record
        for name in path[:-1]:
            target = target.setdefault(name, {})
        # SQL returns the original numeric token. Keep fractional signs until the
        # chosen field's validity check; JSON integers retain their exact int type.
        target[path[-1]] = json.loads(value, parse_float=Decimal) if value is not None else None
    return record


async def daily_summaries(session: AsyncSession, first: date, last: date) -> list[dict[str, Any]]:
    """Keep one day's projected inputs and returned summaries, never samples or history.

    Current observations are visible immediately, even before normalization publishes
    them. The shared version selector retains observation ties and legacy max-id keys.
    """
    rows = await session.stream(summary_query(first, last).execution_options(yield_per=100))
    days: list[dict[str, Any]] = []
    day, records = None, {}
    try:
        async for key, kind, present, *values in rows.tuples():
            if day is not None and key != day:
                days.append(day_summary(day, records))
                records = {}
            day = key
            records[kind] = _record(kind, present, tuple(values))
        if day is not None:
            days.append(day_summary(day, records))
    finally:
        await rows.close()
    return days


def _number(value: Any) -> int | float | None:
    """Convert internal Decimal tokens only at the API's numeric boundary.

    Unrestricted fields retain their existing float conversion and finite-number
    policy. No Decimal or numeric string may escape into the response.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _nonnegative(value: Any) -> int | float | None:
    """Check the source sign before float underflow, accepting signed zero."""
    if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
        return None
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _get(record: Any, *path: str) -> Any:
    value = record
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def day_summary(day: str, records: dict[str, Any]) -> dict[str, Any]:
    steps, summary, spo2 = (records.get(name) for name in ("steps", "daily-summaries", "spo2"))
    # Sleep and HRV fall back by whole record, even when a present primary has no value.
    sleep = records.get("sleep") or records.get("sleep-summaries")
    hrv = records.get("hrv-summaries") or _get(records.get("hrv"), "hrvSummary")
    # Select by the existing numeric policy, but retain the source value for its
    # later sign check. A negative primary must block a valid alternate, whereas
    # a missing, nonnumeric or nonfinite primary still permits that fallback.
    resting = _get(summary, "heartRate", "restingValue")
    if _number(resting) is None:
        resting = _get(records.get("heart-rate"), "restingHeartRate")
    stress = _number(_get(records.get("stress"), "values", "overallStressLevel"))
    if stress is None:
        stress = _number(_get(summary, "stress", "avgLevel"))
    return {
        "date": day,
        "steps": (
            None
            if negative_steps(_get(steps, "totalSteps"))
            else _number(_get(steps, "totalSteps"))
        ),
        "distance_m": _number(_get(steps, "totalDistance")),
        "resting_heart_rate": _nonnegative(resting),
        "sleep_seconds": _nonnegative(_get(sleep, "sleepTimeSeconds")),
        "sleep_stages_seconds": {
            "deep": _nonnegative(_get(sleep, "deepSleepSeconds")),
            "light": _nonnegative(_get(sleep, "lightSleepSeconds")),
            "rem": _nonnegative(_get(sleep, "remSleepSeconds")),
            "awake": _nonnegative(_get(sleep, "awakeSleepSeconds")),
        },
        "sleep_score": _number(_get(sleep, "sleepScores", "overall", "value")),
        "body_battery": {
            "max": _number(_get(summary, "bodyBattery", "maxValue")),
            "min": _number(_get(summary, "bodyBattery", "minValue")),
            "latest": _number(_get(summary, "bodyBattery", "latestValue")),
            "charged": _number(_get(summary, "bodyBattery", "chargedValue")),
            "drained": _number(_get(summary, "bodyBattery", "drainedValue")),
        },
        "stress_average": stress,
        "hrv_last_night_average": _nonnegative(_get(hrv, "lastNightAvg")),
        "spo2_average": _nonnegative(_get(spo2, "averageSpO2")),
    }
