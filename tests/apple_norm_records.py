"""Synthetic Apple Health records for the normalization/reader tests (tests/test_apple_*.py).

Built directly as ``AppleRecord`` values in the body shapes the parsers produce (export.xml
attributes as text; Health Auto Export ``{"metric", "units", "aggregation", "point"}`` and
``{"workout", "version"}``), so these tests do not depend on the parsers. Source names and
values are made up; no value here is real health data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.apple.records import (
    ACTIVITY_SUMMARY,
    CATEGORY,
    EXPORT,
    HAE,
    HAE_METRIC,
    HAE_WORKOUT,
    QUANTITY,
    RECORD,
    WORKOUT,
    AppleRecord,
    body_digest,
    identity_key,
    parse_apple_date,
)

WATCH, PHONE = "Test Watch", "Test iPhone"
WATCH_DEVICE = (
    "<<HKDevice: 0x1>, name:Apple Watch, manufacturer:Apple Inc., model:Watch, "
    "hardware:Watch6,1, software:10.1>"
)
PHONE_DEVICE = "<<HKDevice: 0x2>, name:iPhone, manufacturer:Apple Inc., model:iPhone>"
PLUS_ONE = timezone(timedelta(hours=1))


def stamp(moment: datetime) -> str:
    """An Apple timestamp (``2026-11-06 07:00:00 +0100``)."""
    return moment.strftime("%Y-%m-%d %H:%M:%S %z")


def _export(kind: str, body: dict[str, Any], record_type: str, source: str, device: str | None):
    return AppleRecord(
        channel=EXPORT,
        kind=kind,
        type=record_type,
        key=body_digest(body),
        body=body,
        start=parse_apple_date(body.get("startDate")),
        end=parse_apple_date(body.get("endDate")),
        source_name=source,
        device=device,
    )


def quantity(
    name: str,
    value: object,
    unit: str,
    start: datetime,
    end: datetime | None = None,
    *,
    source: str = WATCH,
    **extra: Any,
) -> AppleRecord:
    """An export ``<Record type="HKQuantityTypeIdentifier<name>">``."""
    end = end or start
    record_type = f"HKQuantityTypeIdentifier{name}"
    body = {
        "type": record_type,
        "sourceName": source,
        "sourceVersion": "10.1",
        "device": WATCH_DEVICE if source == WATCH else PHONE_DEVICE,
        "unit": unit,
        "creationDate": stamp(end),
        "startDate": stamp(start),
        "endDate": stamp(end),
        "value": str(value),
        **extra,
    }
    device = "Apple Watch" if source == WATCH else "iPhone"
    return _export(QUANTITY, body, record_type, source, device)


def category(
    name: str, value: str, start: datetime, end: datetime, *, source: str = WATCH
) -> AppleRecord:
    """An export ``<Record type="HKCategoryTypeIdentifier<name>" value="...">``."""
    record_type = f"HKCategoryTypeIdentifier{name}"
    body = {
        "type": record_type,
        "sourceName": source,
        "creationDate": stamp(end),
        "startDate": stamp(start),
        "endDate": stamp(end),
        "value": value,
    }
    device = "Apple Watch" if source == WATCH else "iPhone"
    return _export(CATEGORY, body, record_type, source, device)


def sleep(stage: str, start: datetime, end: datetime, *, source: str = WATCH) -> AppleRecord:
    return category(
        "SleepAnalysis", f"HKCategoryValueSleepAnalysis{stage}", start, end, source=source
    )


def correlation(start: datetime) -> AppleRecord:
    record_type = "HKCorrelationTypeIdentifierBloodPressure"
    body = {
        "type": record_type,
        "sourceName": PHONE,
        "startDate": stamp(start),
        "endDate": stamp(start),
        "records": ["a" * 64, "b" * 64],
    }
    return _export(RECORD, body, record_type, PHONE, None)


def workout(
    activity: str,
    start: datetime,
    end: datetime,
    *,
    source: str = WATCH,
    statistics: list[dict[str, Any]] | None = None,
    **attributes: Any,
) -> AppleRecord:
    """An export ``<Workout workoutActivityType="HKWorkoutActivityType<activity>">``."""
    record_type = f"HKWorkoutActivityType{activity}"
    body = {
        "workoutActivityType": record_type,
        "sourceName": source,
        "device": WATCH_DEVICE,
        "creationDate": stamp(end),
        "startDate": stamp(start),
        "endDate": stamp(end),
        **attributes,
        "metadata": {},
        "events": [],
        "statistics": statistics or [],
        "routes": [],
    }
    return _export(WORKOUT, body, record_type, source, "Apple Watch")


def activity_summary(day: str, **attributes: str) -> AppleRecord:
    body = {"dateComponents": day, **attributes}
    return AppleRecord(
        channel=EXPORT,
        kind=ACTIVITY_SUMMARY,
        type="ActivitySummary",
        key=identity_key("ActivitySummary", day),
        body=body,
    )


def hae_metric(
    name: str,
    units: str | None,
    point: dict[str, Any],
    *,
    aggregation: str | None = None,
    version: str = "1",
) -> AppleRecord:
    """A Health Auto Export ``data.metrics[]`` point (kind ``hae-metric``)."""
    body = {"metric": name, "units": units, "aggregation": aggregation, "point": point}
    start = next(
        (
            parse_apple_date(point[field])
            for field in ("start", "startDate", "sleepStart", "inBedStart", "date")
            if field in point
        ),
        None,
    )
    end = next(
        (
            parse_apple_date(point[field])
            for field in ("end", "endDate", "sleepEnd", "inBedEnd")
            if field in point
        ),
        None,
    )
    source = point.get("source")
    key = identity_key(
        name, point.get("date"), point.get("start"), point.get("value"), source, version
    )
    return AppleRecord(
        channel=HAE,
        kind=HAE_METRIC,
        type=name,
        key=key,
        body=body,
        start=start,
        end=end,
        source_name=source if isinstance(source, str) else None,
    )


def hae_workout(workout_body: dict[str, Any], version: str = "2") -> AppleRecord:
    body = {"workout": workout_body, "version": version}
    source = workout_body.get("source")
    name = source.get("name") if isinstance(source, dict) else None
    return AppleRecord(
        channel=HAE,
        kind=HAE_WORKOUT,
        type=str(workout_body.get("name") or "workout"),
        key=identity_key(workout_body.get("id") or workout_body.get("start"), version),
        body=body,
        start=parse_apple_date(workout_body.get("start")),
        end=parse_apple_date(workout_body.get("end")),
        source_name=name,
    )
