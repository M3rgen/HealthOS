"""Sparse settings responses: absent preferences stay absent, including nulls."""

from datetime import datetime
from typing import TypedDict


class Appearance(TypedDict):
    selection: dict[str, str]
    updated_at: datetime | None


class Display(TypedDict):
    schema_version: int
    preferences: dict[str, str | bool]
    updated_at: datetime | None
