"""Provenance and lock-time stamps of an archived page write."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StoreProvenance:
    origin: str
    response_id: int | None
    fetched_at: datetime | None
    fetch_started: datetime | None = None


@dataclass(frozen=True, slots=True)
class WriteStamp:
    now: datetime
    origin: str
    response_id: int | None
