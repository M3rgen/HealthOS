"""Per-record alias and supersession planning for identity backfills."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.records import RecordType
from app.models.identity_tables import RawHealthDataSupersession
from app.parsers.identity import new_record_id, reading, supersession_reason

from .identity_backfill_agreement import tally_agreement
from .identity_backfill_reading import EPOCH, Row
from .identity_backfill_state import chunked
from .identity_backfill_stats import BackfillStats
from .identity_common import aliases, among
from .source_chain_model import Chains


@dataclass
class Plan:
    aliases: dict[str, Row] = field(default_factory=dict)
    links: list[tuple[Row, Row, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Link:
    old: uuid.UUID
    new: uuid.UUID
    old_source: str
    new_source: str
    created: datetime


async def _links_of(session: AsyncSession, ids: Sequence[uuid.UUID]) -> list[_Link]:
    """Every supersession whose old or new row is one of ``ids`` (any state)."""
    link = RawHealthDataSupersession
    found: dict[tuple[uuid.UUID, uuid.UUID], _Link] = {}
    for chunk in chunked(sorted(ids), 5000):
        for column in (link.old_raw_id, link.new_raw_id):
            rows = await session.execute(
                select(
                    link.old_raw_id,
                    link.new_raw_id,
                    link.old_fingerprint,
                    link.new_fingerprint,
                    link.created_at,
                ).where(among(session, column, chunk))
            )
            for old, new, old_source, new_source, created in rows.tuples():
                found[(old, new)] = _Link(old, new, old_source, new_source, created)
    return list(found.values())


@dataclass(frozen=True, slots=True)
class ChainSettlement:
    chains: Chains
    stats: BackfillStats


@dataclass(frozen=True, slots=True)
class SettlementRows:
    starts: Sequence[datetime]
    extra: Sequence[uuid.UUID]


async def plan_batch(
    session: AsyncSession,
    spec: RecordType,
    rows: list[Row],
    chains: Chains,
    stats: BackfillStats,
    *,
    count_groups: bool = True,
) -> Plan:
    """Per key and device, in two steps:

    1. per fingerprint, its *root* (live first, then the one seen last): every other row of
       that fingerprint not yet superseded by one of its own is linked to it (a duplicate, an
       older shape or value of what the source returns; such a link is always in force);
    2. per device, its *holder* among the roots that are counted (live first, the one the
       others already link to, the one that already holds others, then the newest): every
       other counted root that measured the same is linked to it (a value of its own is
       never hidden: ``kept_differing``).

    So only roots get links across fingerprints: when a chain is rejected or refuted, each
    fingerprint still counts once, and two forms joined later are linked root to root. The
    rows a newly linked row held are linked to its holder too (one step for readers), where
    they are of the holder's fingerprint or measured the same. A link that would close a
    cycle of links (in any state) is not made (``conflicts``). Links no longer in force are
    history: their rows are settled again like any other."""
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        groups[row.identity.match].append(row)
    alias = await aliases(session, spec.name, {row.identity.specific_key for row in rows})
    links = await _links_of(session, [row.raw_id for row in rows])
    planner = _Planner(spec, rows, chains, stats, links)
    for members in groups.values():
        planner.group(members, alias, count_groups)
    return planner.plan


class _Planner:
    """Mutable graph and counters for one already-loaded backfill batch."""

    def __init__(
        self,
        spec: RecordType,
        rows: list[Row],
        chains: Chains,
        stats: BackfillStats,
        links: list[_Link],
    ) -> None:
        self.spec, self.chains, self.stats = spec, chains, stats
        self.plan = Plan()
        self.by_id = {row.raw_id: row for row in rows}
        self.existing = {(link.old, link.new) for link in links}
        self.edges: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        # Rows superseded by a row of their own fingerprint (always in force), rows superseded
        # in force by a counted row (hidden from readers), and the rows each row holds in force.
        self.own_superseded: set[uuid.UUID] = set()
        self.hidden: set[uuid.UUID] = set()
        self.dependents: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for link in links:
            self.edges[link.old].add(link.new)
            if link.old_source == link.new_source:
                self.own_superseded.add(link.old)
            if self.chains.in_force(link.old_source, link.new_source):
                self.dependents[link.new].append(link.old)
                holder = self.by_id.get(link.new)
                # Hidden like readers see it (held_elsewhere): its holder is neither marked
                # deleted nor quarantined.
                if holder is None or not (holder.deleted or holder.quarantined):
                    self.hidden.add(link.old)
        self.pointed = set(self.dependents)

    def reaches(self, start: uuid.UUID, target: uuid.UUID) -> bool:
        seen, frontier = {start}, [start]
        while frontier:
            for following in self.edges.get(frontier.pop(), ()):
                if following == target:
                    return True
                if following not in seen:
                    seen.add(following)
                    frontier.append(following)
        return False

    def measured(self, row: Row) -> str | None:
        return reading(self.spec, row.raw_json)

    def newest_form(self, row: Row) -> str:
        span = self.chains.spans.get(row.identity.source)
        return span.first.isoformat() if span is not None else ""

    def add_link(self, row: Row, holder: Row) -> None:
        self.plan.links.append(
            (row, holder, supersession_reason(self.spec, row.raw_json, holder.raw_json))
        )
        self.edges[row.raw_id].add(holder.raw_id)
        self.existing.add((row.raw_id, holder.raw_id))
        self.dependents[holder.raw_id].append(row.raw_id)
        self.hidden.add(row.raw_id)
        # What the row held, the holder holds now (one step for readers): rows of the
        # holder's fingerprint, and rows of others that measured the same.
        for dependent in list(self.dependents.get(row.raw_id, ())):
            held = self.by_id.get(dependent)
            if (
                held is not None
                and not held.deleted
                and (
                    held.identity.source == holder.identity.source
                    or (
                        self.measured(held) == self.measured(holder)
                        and held.identity.compatible(holder.identity)
                    )
                )
                and (dependent, holder.raw_id) not in self.existing
                and not self.reaches(holder.raw_id, dependent)
            ):
                self.plan.links.append(
                    (held, holder, supersession_reason(self.spec, held.raw_json, holder.raw_json))
                )
                self.edges[dependent].add(holder.raw_id)
                self.existing.add((dependent, holder.raw_id))
                self.dependents[holder.raw_id].append(dependent)

    def join(self, row: Row, holder: Row) -> bool:
        if (row.raw_id, holder.raw_id) in self.existing:
            return True
        if not row.identity.compatible(holder.identity):
            # Never one record (two values of a device field): no link ever joins them, whatever
            # the chains say (listed with the conflicts; both count).
            self.stats.conflicts += not row.deleted
            return False
        if self.reaches(holder.raw_id, row.raw_id):
            # Left double (listed) unless the row is marked deleted: then it counts nowhere.
            self.stats.conflicts += not row.deleted
            return False
        self.add_link(row, holder)
        return True

    def group(self, members: list[Row], alias: dict[str, uuid.UUID], count_groups: bool) -> None:
        if count_groups:
            self.stats.identities += 1
        devices: dict[str, list[Row]] = defaultdict(list)
        for row in members:
            devices[self.chains.device(row.identity.source)].append(row)
        if count_groups:
            if len(devices) > 1:
                self.stats.distinct_sources += 1
        tally_agreement(self.spec, members, self.chains, self.stats)
        for device in devices.values():
            self.device(device, members, count_groups)
        self.aliases(members, alias, count_groups)

    def device(self, device: list[Row], members: list[Row], count_groups: bool) -> None:
        if count_groups:
            self.stats.already_superseded += sum(row.raw_id in self.hidden for row in device)
        by_source: dict[str, list[Row]] = defaultdict(list)
        for row in device:
            by_source[row.identity.source].append(row)
        roots: list[Row] = []
        for own in by_source.values():
            candidates = [row for row in own if row.raw_id not in self.own_superseded] or own
            root = max(
                candidates,
                key=lambda row: (
                    not row.deleted,
                    # A growing interval: the end returned last holds the record.
                    row.last_seen_at if row.identity.shape else EPOCH,
                    row.identity.shape or "",
                    row.last_seen_at,
                    row.raw_id in self.pointed,
                    row.first_seen_at,
                    str(row.raw_id),
                ),
            )
            for row in candidates:
                if row is not root:
                    self.join(row, root)
            roots.append(root)
        self.cross_source(roots)
        self.deletions(device, members, count_groups)

    def cross_source(self, roots: list[Row]) -> None:
        # A root marked deleted counts nowhere, and a link would keep it hidden when its
        # record comes back with another value (a row of its own again, every time):
        # only live roots are linked across fingerprints.
        counted = [root for root in roots if root.raw_id not in self.hidden and not root.deleted]
        if len(counted) > 1:
            holder = max(
                counted,
                key=lambda row: (
                    # A growing interval: the end returned last holds the record (an end
                    # Google no longer returns is stale, longer or not).
                    row.last_seen_at if row.identity.shape else EPOCH,
                    row.identity.shape or "",
                    sum((other.raw_id, row.raw_id) in self.existing for other in counted),
                    row.raw_id in self.pointed,
                    row.last_seen_at,
                    # Of rows returned together, the device's newest form (the one
                    # returned again, as a rule).
                    self.newest_form(row),
                    row.first_seen_at,
                    str(row.raw_id),
                ),
            )
            value = self.measured(holder)
            for root in counted:
                if root is holder:
                    continue
                if self.measured(root) != value:
                    # Another fingerprint that measured something else: never hidden.
                    self.stats.kept_differing += 1
                    continue
                self.join(root, holder)

    def deletions(self, device: list[Row], members: list[Row], count_groups: bool) -> None:
        if count_groups and all(row.deleted for row in device):
            holder = max(device, key=lambda row: row.order)
            others = [
                row
                for row in members
                if row not in device
                and not row.deleted
                and any(row.identity.kindred(own.identity) for own in device)
                and not self.chains.distinct(row.identity.source, holder.identity.source)
            ]
            if others:
                self.stats.deletions_suspicious += 1
            else:
                self.stats.deletions_kept += 1

    def aliases(self, members: list[Row], alias: dict[str, uuid.UUID], count_groups: bool) -> None:
        # Every row reachable by its key and fingerprint: its fingerprint's root first.
        for row in sorted(
            members,
            key=lambda row: (row.raw_id not in self.own_superseded, row.order),
            reverse=True,
        ):
            name = row.identity.specific_key
            if row.google_data_point_id == new_record_id(name):
                continue
            if name in alias:
                if count_groups:
                    self.stats.aliases_present += 1
                continue
            self.plan.aliases.setdefault(name, row)
