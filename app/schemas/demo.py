"""Responses of the synthetic sample-data routes (app/api/demo.py)."""

from datetime import date
from typing import TypedDict


class SampleStatus(TypedDict):
    loaded: bool
    rows: int
    first_day: date | None
    last_day: date | None


class SampleLoad(SampleStatus):
    added: dict[str, int]


class SampleRemoval(SampleStatus):
    removed: dict[str, int]
