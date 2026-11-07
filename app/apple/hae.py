"""Records of a Health Auto Export (iOS app) REST API request body.

The app posts ``{"data": {"metrics": [...], "workouts": [...], ...}}`` (format documented at
https://help.healthyapps.dev/en/health-auto-export/export-format; the parser follows the
documented field names, it was not checked against a real device's payload). Each metric
data point becomes one record (kind ``hae-metric``, type the metric's snake_case ``name``)
and each workout one record (kind ``hae-workout``). Every other array (``stateOfMind``,
``ecg``, ``medications``, ``symptoms``, ``cycleTracking``, ``heartRateNotifications`` and
anything newer) is counted in ``skipped``: the receiver keeps the whole body in
``apple_payloads``, so nothing is lost. Malformed items are counted in ``invalid``; only a
body that is not a JSON object with a ``data`` object is refused.

Identity: a metric point's key is built from its metric, the automation's aggregation
header and the point's time fields and source (and a text ``value``, as sleep stages use),
never from its quantities. The app re-sends a running day total with a larger ``qty``:
same key, another body digest, so the archive keeps it as a newer version.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.apple.records import (
    HAE,
    HAE_METRIC,
    HAE_WORKOUT,
    AppleRecord,
    identity_key,
    parse_apple_date,
)
from app.google_health.strict_json import StrictJsonError, loads_object

AGGREGATION_HEADER = "automation-aggregation"
_START_FIELDS = ("start", "startDate", "sleepStart", "inBedStart", "date")
_END_FIELDS = ("end", "endDate", "sleepEnd", "inBedEnd")


class HaeFormatError(ValueError):
    """The body is not a Health Auto Export JSON payload."""


@dataclass
class HaeResult:
    records: list[AppleRecord]
    counts: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    invalid: int = 0


def _first_date(point: Mapping[str, Any], names: tuple[str, ...]) -> datetime | None:
    """The first of the named fields that holds an Apple timestamp."""
    for name in names:
        parsed = parse_apple_date(point.get(name))
        if parsed is not None:
            return parsed
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _load(body: bytes) -> dict[str, Any]:
    if b"\x00" in body:
        raise HaeFormatError("Health Auto Export body is not UTF-8 JSON")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise HaeFormatError("Health Auto Export body is not UTF-8 JSON") from None
    try:
        return loads_object(body, allow_error=True)
    except StrictJsonError as error:
        raise HaeFormatError(f"Health Auto Export body is not valid JSON ({error.kind})") from None


def parse_hae(body: bytes, headers: Mapping[str, str] | None = None) -> HaeResult:
    """The records of one request body (see the module docstring)."""
    document = _load(body)
    data = document.get("data")
    if not isinstance(data, dict):
        raise HaeFormatError("Health Auto Export body has no 'data' object")
    aggregation = _header(headers, AGGREGATION_HEADER)
    result = HaeResult(records=[])
    for name, value in data.items():
        if name == "metrics":
            _metrics(value, aggregation, result)
        elif name == "workouts":
            _workouts(value, result)
        else:
            result.skipped[name] = result.skipped.get(name, 0) + (
                len(value) if isinstance(value, list) else 1
            )
    return result


def _add(result: HaeResult, record: AppleRecord) -> None:
    result.records.append(record)
    result.counts[record.kind] = result.counts.get(record.kind, 0) + 1


def _metrics(value: object, aggregation: str | None, result: HaeResult) -> None:
    if not isinstance(value, list):
        result.invalid += 1
        return
    for metric in value:
        if not isinstance(metric, dict):
            result.invalid += 1
            continue
        name = metric.get("name")
        points = metric.get("data")
        if not isinstance(name, str) or not name.strip() or not isinstance(points, list):
            result.invalid += 1
            continue
        units = metric.get("units")
        for point in points:
            if not isinstance(point, dict):
                result.invalid += 1
                continue
            key = identity_key(
                name,
                aggregation,
                point.get("date"),
                point.get("start"),
                point.get("end"),
                point.get("startDate"),
                point.get("endDate"),
                point.get("sleepStart"),
                point.get("inBedStart"),
                point.get("source"),
                _text(point.get("value")),
            )
            _add(
                result,
                AppleRecord(
                    channel=HAE,
                    kind=HAE_METRIC,
                    type=name,
                    key=key,
                    body={
                        "metric": name,
                        "units": units,
                        "aggregation": aggregation,
                        "point": point,
                    },
                    start=_first_date(point, _START_FIELDS),
                    end=_first_date(point, _END_FIELDS),
                    source_name=_text(point.get("source")),
                ),
            )


def _workouts(value: object, result: HaeResult) -> None:
    if not isinstance(value, list):
        result.invalid += 1
        return
    for workout in value:
        if not isinstance(workout, dict):
            result.invalid += 1
            continue
        name = workout.get("name")
        if not isinstance(name, str) or not name.strip():
            result.invalid += 1
            continue
        workout_id = workout.get("id")
        start, end = workout.get("start"), workout.get("end")
        if workout_id is not None and workout_id != "":
            key = identity_key("id", workout_id)
        else:
            key = identity_key(name, start, end)
        source = workout.get("source")
        source_name = _text(source.get("name")) if isinstance(source, dict) else _text(source)
        _add(
            result,
            AppleRecord(
                channel=HAE,
                kind=HAE_WORKOUT,
                type=name,
                key=key,
                body={
                    "workout": workout,
                    "version": "2" if "id" in workout or "duration" in workout else "1",
                },
                start=parse_apple_date(start),
                end=parse_apple_date(end),
                source_name=source_name,
            ),
        )
