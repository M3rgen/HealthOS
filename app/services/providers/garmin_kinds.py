"""Garmin Personal API endpoint families and their record keys."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.garmin.client import GarminError

from .garmin_activity_identity import activity_key

ACTIVITY_PAGE = 100


class GarminResponseShapeError(GarminError):
    """A list endpoint answered with an unsupported outer JSON structure.

    Keep the error independent of the body: per-kind reporting persists and logs
    its message. HTTP evidence is archived separately, with credential masking.
    """

    def __init__(self) -> None:
        super().__init__("Garmin list response has an unexpected structure")


class GarminSingleResponseShapeError(GarminError):
    """A single-day endpoint answered with an unsupported outer JSON structure.

    This message is persisted and logged by the per-kind failure path. Keep it
    independent of response contents; the existing HTTP evidence archive applies
    credential masking separately before storing the rejected response.
    """

    def __init__(self) -> None:
        super().__init__("Garmin single-day response has an unexpected structure")


@dataclass(frozen=True)
class Kind:
    """One Garmin endpoint family and how its records are keyed.

    ``mode``: ``range`` (one request per chunk of at most ``max_days`` inclusive days),
    ``day`` (one request per calendar day) or ``search`` (paged activity search over a
    range). ``request(start, end)`` gives the path and query parameters; ``records(body)``
    the records of a response; ``key(record)`` the record's calendar day or id (``None``:
    fall back to the requested day, else to the content digest).
    """

    name: str
    mode: str
    max_days: int
    request: Callable[[date, date], tuple[str, dict[str, Any]]]
    records: Callable[[Any], list]
    key: Callable[[Any], str | None]


def iso(day: date) -> str:
    return day.isoformat()


def _list(body: Any) -> list:
    """Accept only an actual list or a bodyless/null response.

    Validate the outer collection, leaving individual records to the existing
    archival and normalization rules, even when their measurement fields are bad.
    """
    if body is None:
        return []
    if isinstance(body, list):
        return list(body)
    raise GarminResponseShapeError()


def _field(body: Any, name: str) -> list:
    """Require the endpoint's named list; a missing/null field is not no data.

    Preserve the supported bare empty list and bodyless/null responses. A populated
    envelope must explicitly carry its list, so errors cannot finish a date chunk.
    """
    if body is None or (isinstance(body, list) and not body):
        return []
    if isinstance(body, dict) and isinstance(body.get(name), list):
        return list(body[name])
    raise GarminResponseShapeError()


def _single(body: Any) -> list:
    """Accept an object or a supported empty response, never a scalar or full list.

    Only the outer structure is checked here. Every populated object enters the
    archive even if it has no usable measurements; normalization owns those rules.
    A malformed outer structure must instead fail the day so backfill can retry it.
    """
    if body is None:
        return []
    if isinstance(body, dict):
        return [body] if body else []
    if isinstance(body, list) and not body:
        return []
    raise GarminSingleResponseShapeError()


def _calendar_date(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    for path in (("calendarDate",), ("date",), ("nextSleepNeed", "calendarDate")):
        value: Any = record
        for name in path:
            value = value.get(name) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            return value[:64]
    return None


def _activity_id(record: Any) -> str | None:
    return activity_key(record)


KINDS: tuple[Kind, ...] = (
    Kind(
        "daily-summaries",
        "range",
        90,
        lambda s, e: (
            "/usersummary-service/v2/dailysummaries",
            {"startDate": iso(s), "endDate": iso(e)},
        ),
        lambda body: _field(body, "data"),
        _calendar_date,
    ),
    Kind(
        "steps",
        "range",
        28,
        lambda s, e: (f"/usersummary-service/stats/steps/daily/{iso(s)}/{iso(e)}", {}),
        _list,
        _calendar_date,
    ),
    Kind(
        "stress",
        "range",
        28,
        lambda s, e: (f"/usersummary-service/stats/stress/daily/{iso(s)}/{iso(e)}", {}),
        _list,
        _calendar_date,
    ),
    Kind(
        "body-battery",
        "range",
        28,
        lambda s, e: (
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": iso(s), "endDate": iso(e)},
        ),
        _list,
        _calendar_date,
    ),
    Kind(
        "sleep-summaries",
        "range",
        31,
        lambda s, e: (
            "/sleep-service/sleep/dailySleepSummariesByDate",
            {"startDate": iso(s), "endDate": iso(e)},
        ),
        _list,
        _calendar_date,
    ),
    Kind(
        "hrv-summaries",
        "range",
        28,
        lambda s, e: (f"/hrv-service/hrv/daily/{iso(s)}/{iso(e)}", {}),
        lambda body: _field(body, "hrvSummaries"),
        _calendar_date,
    ),
    Kind(
        "heart-rate",
        "day",
        1,
        lambda s, e: ("/wellness-service/wellness/dailyHeartRate", {"date": iso(s)}),
        _single,
        lambda record: None,
    ),
    Kind(
        "spo2",
        "day",
        1,
        lambda s, e: (f"/wellness-service/wellness/daily/spo2acclimation/{iso(s)}", {}),
        _single,
        lambda record: None,
    ),
    Kind(
        "sleep",
        "day",
        1,
        lambda s, e: ("/sleep-service/sleep/dailySleep", {"date": iso(s)}),
        _single,
        lambda record: None,
    ),
    Kind(
        "hrv",
        "day",
        1,
        lambda s, e: (f"/hrv-service/hrv/{iso(s)}", {}),
        _single,
        lambda record: None,
    ),
    Kind(
        "activities",
        "search",
        90,
        lambda s, e: (
            "/activitylist-service/activities/search/activities",
            {"startDate": iso(s), "endDate": iso(e), "limit": ACTIVITY_PAGE},
        ),
        _list,
        _activity_id,
    ),
)
KIND_NAMES = tuple(kind.name for kind in KINDS)
BY_NAME = {kind.name: kind for kind in KINDS}
