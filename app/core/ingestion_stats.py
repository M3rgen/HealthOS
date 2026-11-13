"""Archive and normalization counters shared by provider ingestion and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizeStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    tables: dict[str, dict[str, int]] = field(default_factory=dict)

    def note(self, table: str, inserted: int, updated: int, unchanged: int) -> None:
        self.inserted += inserted
        self.updated += updated
        self.unchanged += unchanged
        self.tables[table] = {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def add(self, other: NormalizeStats) -> None:
        """Add one bounded transaction's counters to the run's."""
        self.skipped += other.skipped
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        for table, counts in other.tables.items():
            mine = self.tables.setdefault(table, dict.fromkeys(counts, 0))
            for name, value in counts.items():
                mine[name] += value

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "tables": self.tables,
        }


@dataclass
class StoreStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    # Records jsonb cannot hold, kept in raw_health_data_rejections instead (never stored).
    rejected: int = 0
    normalized: int = 0
    normalization_skipped: int = 0
    # Archived records a complete window re-fetch no longer returned (see source_deletions).
    source_deleted: int = 0
    # Replaced payloads preserved in raw_health_data_versions.
    versions_kept: int = 0

    def add(self, other: StoreStats) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))
