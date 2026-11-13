"""Internal dictionaries retained between interpretation planning and validation."""

from datetime import date
from typing import Any, NotRequired, TypedDict


class EvidenceData(TypedDict):
    record: str
    date: str
    value: float | None
    start: str | None
    end: str | None
    source_timezone: NotRequired[str | None]
    field: str | None
    reason_codes: list[str]


class SeriesRow(TypedDict):
    value: float | None
    reasons: list[str]
    comparable: bool
    method_details: dict[str, Any]
    evidence: list[EvidenceData]


type SeriesKey = tuple[str, str, str, str, str, str]
type DailySeries = dict[SeriesKey, dict[date, SeriesRow]]
# Canonical day/group dictionaries are validated by CanonicalDay after assembly.
type Group = dict[str, Any]
type Groups = dict[tuple[str, str, str], Group]


class FrameData(TypedDict):
    metric: str
    source: str
    definition: str
    unit: str
    method_version: str
    time_basis: str
