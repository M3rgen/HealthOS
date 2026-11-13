"""Value objects shared by identity decisions and archive reconciliation."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .identity_backfill_stats import BackfillStats


@dataclass(frozen=True, slots=True)
class BackfillOptions:
    batch_size: int = 5_000
    dry_run: bool = False
    statement_timeout: str = "120s"
    progress: Callable[[str, BackfillStats, datetime], None] | None = None
    notice: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True)
class SourceDecision:
    """Owner's decision and fingerprint names (prefixes at the command boundary)."""

    decision: str
    first: str
    second: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ChainEvidence:
    decided_by: str
    now: datetime
    shared_keys: int | None = None
    decision_id: int | None = None


DEFAULT_BACKFILL_OPTIONS = BackfillOptions()
