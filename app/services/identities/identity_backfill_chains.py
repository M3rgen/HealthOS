"""Archive timelines and source-chain reconciliation for identity backfills."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import event
from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.raw_versions import RawHealthDataVersion
from app.parsers.common import google_name
from app.parsers.identity import RecordIdentity, instant, reading, record_identity

from . import identity_backfill_reading as archive_reading
from . import source_chain_repository
from .identity_backfill_reporting import Timeline, report, together
from .identity_backfill_state import (
    DeviceChange,
    Exclusive,
    deferred_of,
    owe_settlement,
    prepare,
)
from .identity_backfill_stats import BackfillStats, fingerprints
from .identity_common import among
from .identity_options import BackfillOptions, ChainEvidence
from .source_chain_evidence import Evidence, gap_seconds, ordered_pair, useful
from .source_chain_model import Chains
from .source_chain_repository import (
    insert_chain,
    load_chains,
    record_devices,
    record_evidence,
    record_refutations,
    write_spans,
)
from .source_chain_rules import Decision, decide_chains

logger = logging.getLogger("app.services.identities.identity_backfill")


async def timeline(
    sessions: async_sessionmaker,
    spec: RecordType,
    batch_size: int,
    statement_timeout: str,
) -> Timeline:
    timeline = Timeline()
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        async with sessions.begin() as session:
            await prepare(session, statement_timeout)
            batch = await archive_reading.read_batch(session, spec.name, batch_size, cursor, None)
            if not batch:
                break
            row_keys: dict[uuid.UUID, tuple[str, datetime]] = {}
            # Match key -> fingerprint -> its identity (whether two are kindred).
            sources: dict[str, dict[str, RecordIdentity]] = defaultdict(dict)
            stored: dict[str, list] = defaultdict(list)
            for raw_id, _, raw_json, start, first, last, _, received, _, _ in batch:
                identity = record_identity(spec, raw_json)
                if google_name(raw_json) is not None or identity is None:
                    continue
                if identity.start is not None and instant(identity.start) != instant(start):
                    timeline.stale.append((as_utc(identity.start), raw_id))
                data_source = raw_json.get("dataSource")
                # The current payload arrived at payload_received_at (NULL before 0011: the
                # row never changed its source then, so its first sighting is the row's).
                timeline.sight(identity, data_source, received or first, last)
                row_keys[raw_id] = (identity.key, first)
                sources[identity.match][identity.source] = identity
                stored[identity.match].append(
                    (as_utc(first), as_utc(last), identity, reading(spec, raw_json))
                )
            if row_keys:
                await _remember_versions(session, spec, timeline, row_keys, sources)
        for found in stored.values():
            for pair, shared_key, seen in sorted(together(found)):
                kept = timeline.evidence.setdefault(pair, [])
                item = Evidence(shared_key, seen)
                if useful(kept, item):
                    kept.append(item)
        for forms in sources.values():
            for left, right in combinations(sorted(forms), 2):
                if forms[left].kindred(forms[right]):
                    timeline.shared[(left, right)] += 1
        cursor = (batch[-1][3], batch[-1][0])
    return timeline


async def _remember_versions(
    session: AsyncSession,
    spec: RecordType,
    timeline: Timeline,
    row_keys: dict[uuid.UUID, tuple[str, datetime]],
    sources: dict[str, dict[str, RecordIdentity]],
) -> None:
    versions = await session.execute(
        select(
            RawHealthDataVersion.raw_id,
            RawHealthDataVersion.raw_json,
            RawHealthDataVersion.received_at,
            RawHealthDataVersion.last_seen_at,
        ).where(among(session, RawHealthDataVersion.raw_id, list(row_keys)))
    )
    for raw_id, raw_json, received, last in versions.tuples():
        identity = record_identity(spec, raw_json)
        if identity is None:
            continue
        key, first = row_keys[raw_id]
        timeline.sight(identity, raw_json.get("dataSource"), received or first, last)
        if identity.key == key:
            sources[identity.match][identity.source] = identity


@dataclass(frozen=True)
class ChainExecution:
    """Transaction executor and optional owner action to run before reconciliation."""

    exclusively: Exclusive
    apply: Callable[[AsyncSession, Chains], Awaitable[None]] | None = None


@dataclass(frozen=True)
class _Settlement:
    spec: RecordType
    timeline: Timeline | None
    stats: BackfillStats
    apply: Callable[[AsyncSession, Chains], Awaitable[None]] | None
    dry_run: bool
    decided_by: str


async def settle_chains(
    sessions: async_sessionmaker,
    spec: RecordType,
    timeline: Timeline | None,
    stats: BackfillStats,
    *,
    exclusively: Exclusive | ChainExecution,
    options: BackfillOptions,
) -> tuple[Chains, DeviceChange]:
    """Merge the spans and evidence (full run), refute, decide and record the chains, and log
    the devices that changed, under the exclusive lock. ``ChainExecution.apply`` runs an
    owner decision first (identity-decide).

    Returns the chains (with a dry run's planned links) and the fingerprint pairs that are
    no longer, or newly, one device (a link of this run, the decision, a refutation)."""
    decided_by = "backfill" if timeline is not None else "reconciler"

    execution = (
        exclusively if isinstance(exclusively, ChainExecution) else ChainExecution(exclusively)
    )
    work = partial(
        _settle,
        settlement=_Settlement(spec, timeline, stats, execution.apply, options.dry_run, decided_by),
    )
    return await execution.exclusively(
        sessions,
        spec.name,
        work,
        options.statement_timeout,
        options.notice,
        dry_run=options.dry_run,
    )


async def _settle(session: AsyncSession, *, settlement: _Settlement) -> tuple[Chains, DeviceChange]:
    spec, timeline, stats = settlement.spec, settlement.timeline, settlement.stats
    apply, dry_run = settlement.apply, settlement.dry_run
    now = await source_chain_repository.database_now(session)
    chains = await load_chains(session, spec.name)
    if timeline is not None:
        await _merge_timeline(session, spec, chains, timeline, now, dry_run)
    names = set(chains.spans) | {name for link in chains.links for name in link}
    members_before = {name: chains.members(name) for name in names}
    if apply is not None:
        await apply(session, chains)
    if dry_run:
        refuted = [(old, new) for old, new in sorted(chains.links) if chains.refutable(old, new)]
        chains.refute(refuted)
    else:
        refuted = await record_refutations(session, spec.name, chains, now)
    decision = _chain_decision(chains, stats, owner_decision=apply is not None)
    recorded = await _record_links(session, chains, decision.links, now, settlement)
    for old, new in refuted:
        event(
            logger,
            "SOURCE_CHAIN_REFUTED",
            data_type=spec.name,
            old=chains.spans[old].label if old in chains.spans else old[:12],
            new=chains.spans[new].label if new in chains.spans else new[:12],
        )
    if decision.ambiguous:
        event(logger, "SOURCE_CHAIN_AMBIGUOUS", data_type=spec.name, count=len(decision.ambiguous))
    if not dry_run:
        # Readers count supersessions by these devices: logged in this same transaction.
        await record_devices(session, spec.name, chains, now)
    report(chains, timeline, recorded, decision.ambiguous, refuted, stats)
    stats.deferred_deletions = await deferred_of(session, spec.name)
    change = DeviceChange()
    for name in names | {name for link in chains.links for name in link}:
        before = members_before.get(name, frozenset((name,)))
        after = chains.members(name)
        change.lapsed.update(ordered_pair(name, other) for other in before - after)
        change.joined.update(ordered_pair(name, other) for other in after - before)
    # Kept with the change itself, so a re-settlement that fails is resumed by the next run
    # (cleared once it finished); a dry run shows what is still owed too, writing nothing.
    change = await owe_settlement(session, spec.name, change, dry_run=dry_run)
    return chains, change


def _chain_decision(chains: Chains, stats: BackfillStats, *, owner_decision: bool) -> Decision:
    if not owner_decision:
        decision = decide_chains(chains)
    else:
        # An owner decision records only itself: chains it makes possible are decided by
        # the next reconciler run (and reviewed like any other), never inside it.
        decision = decide_chains(chains.copy())
        stats.left_to_reconciler.extend(
            {
                "from": chains.spans[old].label,
                "to": chains.spans[new].label,
                "fingerprints": fingerprints(old, new),
                "why": "the chain rule would link them now: left to the next reconciler run "
                "(identity-backfill --incremental, nightly), which lists it for review",
            }
            for old, new in decision.links
        )
        decision.links = []
    return decision


async def _record_links(
    session: AsyncSession,
    chains: Chains,
    links: list[tuple[str, str]],
    now: datetime,
    settlement: _Settlement,
) -> set[tuple[str, str]]:
    spec, timeline = settlement.spec, settlement.timeline
    dry_run, decided_by = settlement.dry_run, settlement.decided_by
    recorded: set[tuple[str, str]] = set()
    for old, new in links:
        if dry_run:
            recorded.add((old, new))
            chains.add(old, new, decided_by=decided_by)
            continue
        shared = None
        if timeline is not None:
            shared = timeline.shared.get(ordered_pair(old, new), 0)
        # decide_chains added the link in memory; record it (or forget it).
        chains.forget(old, new)
        if await insert_chain(
            session,
            spec.name,
            chains,
            old,
            new,
            evidence_record=ChainEvidence(decided_by=decided_by, now=now, shared_keys=shared),
        ):
            recorded.add((old, new))
            event(
                logger,
                "SOURCE_CHAIN_LINKED",
                data_type=spec.name,
                old=chains.spans[old].label,
                new=chains.spans[new].label,
                gap_seconds=gap_seconds(chains.spans[old], chains.spans[new]),
            )
    return recorded


async def _merge_timeline(
    session: AsyncSession,
    spec: RecordType,
    chains: Chains,
    timeline: Timeline,
    now: datetime,
    dry_run: bool,
):
    for fingerprint, span in timeline.spans.items():
        stored = chains.spans.get(fingerprint)
        if stored is not None:
            span.widen(stored.first, stored.last, stored.newest)
        chains.set_span(fingerprint, span)
    chains.complete_spans()
    found = [
        (*pair, item.key, item.seen) for pair, items in timeline.evidence.items() for item in items
    ]
    if dry_run:
        for left, right, key, seen in found:
            chains.add_evidence(left, right, key, seen)
    else:
        await write_spans(session, spec.name, timeline.spans, now)
        await record_evidence(session, spec.name, chains, found)
