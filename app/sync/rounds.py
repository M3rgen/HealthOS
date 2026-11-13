"""Shared invocation counters and the timing of a single fetch attempt."""

from dataclasses import dataclass
from datetime import datetime

from app.db.contracts import TransactionSessions
from app.google_health.pagination import PageBudget
from app.services.archive.storage import StoreStats


@dataclass(frozen=True, slots=True)
class SyncRun:
    data_type: str
    sessions: TransactionSessions
    total: StoreStats
    budget: PageBudget


@dataclass(slots=True)
class FetchTiming:
    round_started: datetime
    fetched_since: datetime | None
