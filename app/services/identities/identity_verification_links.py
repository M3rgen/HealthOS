"""Read-only supersession checks and their ordered diagnostic counts."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.parsers.common import google_name
from app.parsers.identity import reading, record_identity

from .hollow_names import holds_hollow_row
from .source_chain_model import Chains


def _supersession_query():
    old_row, new_row = aliased(RawHealthData), aliased(RawHealthData)
    link = RawHealthDataSupersession
    links = (
        select(
            link.old_raw_id,
            link.new_raw_id,
            link.data_type,
            link.old_fingerprint,
            link.new_fingerprint,
            old_row.raw_json,
            new_row.raw_json,
            old_row.source_deleted_at,
            new_row.source_deleted_at,
            old_row.last_seen_at,
            new_row.last_seen_at,
            new_row.is_quarantined,
        )
        .join(old_row, old_row.id == link.old_raw_id)
        .join(new_row, new_row.id == link.new_raw_id)
    )
    return links


@dataclass
class _SupersessionChecks:
    errors: list[str] = field(default_factory=list)
    differing: int = 0
    stale: int = 0
    void: int = 0
    # Rows superseded in force, and the holders they point at.
    superseded_rows: set[uuid.UUID] = field(default_factory=set)
    targets: set[uuid.UUID] = field(default_factory=set)

    def check(self, spec: RecordType, row: Sequence[Any], current: Chains) -> None:
        raw_id, holder_id, data_type, old_source, new_source = row[:5]
        old_json, new_json, old_deleted, new_deleted, old_seen, new_seen, held_aside = row[5:]
        if google_name(old_json) is not None:
            if old_source == new_source and holds_hollow_row(spec, old_json, new_json):
                # A row of a hollow name, held by the row of its own record (always in force).
                self.superseded_rows.add(raw_id)
                self.targets.add(holder_id)
                return
            self.errors.append(f"supersession of raw row {raw_id} hides a record with a Google id")
        old, new = record_identity(spec, old_json), record_identity(spec, new_json)
        if old is None or new is None or old.match != new.match or not old.compatible(new):
            self.errors.append(f"supersession of raw row {raw_id} links two different records")
            return
        if not (
            current.ever_joined(old_source, new_source)
            and current.ever_joined(old.source, old_source)
            and current.ever_joined(new.source, new_source)
        ):
            self.errors.append(f"supersession of raw row {raw_id} links two different sources")
        if not current.in_force(old_source, new_source):
            self.void += 1
            return
        self.superseded_rows.add(raw_id)
        self.targets.add(holder_id)
        if old_deleted is None and new_deleted is None:
            if reading(spec, old_json) != reading(spec, new_json):
                self.differing += 1
            if (
                not held_aside
                and as_utc(old_seen) > as_utc(new_seen)
                and (reading(spec, old_json), old.shape) != (reading(spec, new_json), new.shape)
            ):
                self.stale += 1

    def notes(self) -> list[str]:
        notes: list[str] = []
        differing, stale, void = self.differing, self.stale, self.void
        if differing:
            notes.append(
                f"{differing} live superseded row(s) hold a value their holder no longer has (an "
                "older value of the record; the next complete fetch of their window marks them)"
            )
        if stale:
            notes.append(
                f"{stale} superseded row(s) were returned after the row hiding them, with another "
                "reading or end: hidden behind a stale holder until a complete fetch of their "
                "window "
                "marks it (a fetch that was not complete leaves it)"
            )
        if void:
            notes.append(
                f"{void} supersession(s) are no longer in force (their two sources are no longer "
                "one "
                "device: a chain was rejected or refuted, or the owner decided them distinct); the "
                "rows they linked count on their own"
            )
        stacked = len(self.targets & self.superseded_rows)
        if stacked:
            notes.append(
                f"{stacked} holder row(s) are superseded themselves (readers follow both links)"
            )

        return notes


async def verify_supersessions(
    session: AsyncSession,
    chains: dict[str, Chains],
    spec_for: Callable[[str], RecordType],
) -> tuple[list[str], list[str]]:
    checks = _SupersessionChecks()
    result = await session.stream(_supersession_query().execution_options(yield_per=1_000))
    async for row in result:
        data_type = row[2]
        checks.check(spec_for(data_type), row, chains.get(data_type) or Chains())
    return checks.errors, checks.notes()
