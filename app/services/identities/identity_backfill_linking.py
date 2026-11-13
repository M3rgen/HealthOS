"""Transactional writes and batch link settlement for identity backfills."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.db.dialect import database, dialect_insert
from app.models.identity_constants import IDENTITY_TABLE, MERGED_EVENT
from app.models.identity_tables import RawHealthDataIdentity, RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataEvent
from app.models.state import RawHealthData
from app.parsers.identity import reading

from . import identity_backfill_reading as archive_reading
from .identity_backfill_agreement import record_agreement
from .identity_backfill_planning import (
    ChainSettlement,
    Plan,
    SettlementRows,
    plan_batch,
)
from .identity_backfill_reading import COLUMNS, rows_of
from .identity_backfill_state import INSERT_CHUNK, Exclusive, chunked
from .identity_backfill_stats import BackfillStats
from .identity_common import among
from .identity_options import BackfillOptions
from .source_chain_model import Chains
from .source_chain_repository import load_chains


async def _write_plan(
    session: AsyncSession, spec: RecordType, plan: Plan, stats: BackfillStats, *, dry_run: bool
) -> None:
    if dry_run:
        for row, _, reason in plan.links:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
        stats.aliases_added += len(plan.aliases)
        return
    await record_agreement(session, spec.name, stats.agreement_keys)
    await _write_aliases(session, spec, plan, stats)
    if not plan.links:
        return
    table = RawHealthDataSupersession.__table__
    values = [
        {
            "old_raw_id": old.raw_id,
            "new_raw_id": new.raw_id,
            "data_type": spec.name,
            "old_google_data_point_id": old.google_data_point_id,
            "new_google_data_point_id": new.google_data_point_id,
            "reason": reason,
            "old_fingerprint": old.identity.source,
            "new_fingerprint": new.identity.source,
            "same_reading": reading(spec, old.raw_json) == reading(spec, new.raw_json),
            "new_payload_hash": new.payload_hash,
        }
        for old, new, reason in sorted(
            plan.links, key=lambda link: (str(link[0].raw_id), str(link[1].raw_id))
        )
    ]
    # RETURNING names exactly the links this run added.
    inserted: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for offset in range(0, len(values), INSERT_CHUNK):
        statement = (
            dialect_insert(session, table)
            .values(values[offset : offset + INSERT_CHUNK])
            .on_conflict_do_nothing(index_elements=["old_raw_id", "new_raw_id"])
            .returning(table.c.old_raw_id, table.c.new_raw_id)
        )
        inserted.update((await session.execute(statement)).tuples().all())
    for row, holder, reason in plan.links:
        if (row.raw_id, holder.raw_id) in inserted:
            stats.linked[reason] += 1
            stats.superseded_marked += row.deleted
    await _write_merge_events(session, spec, plan, inserted)


async def _write_aliases(
    session: AsyncSession, spec: RecordType, plan: Plan, stats: BackfillStats
) -> None:
    if plan.aliases:
        values = [
            {
                "data_type": spec.name,
                "identity_key": key,
                "raw_id": row.raw_id,
                "google_data_point_id": row.google_data_point_id,
            }
            for key, row in sorted(plan.aliases.items())
        ]
        table = RawHealthDataIdentity.__table__
        for offset in range(0, len(values), INSERT_CHUNK):
            statement = (
                dialect_insert(session, table)
                .values(values[offset : offset + INSERT_CHUNK])
                .on_conflict_do_nothing(index_elements=["data_type", "identity_key"])
                .returning(table.c.identity_key)
            )
            stats.aliases_added += len((await session.execute(statement)).scalars().all())


async def _write_merge_events(
    session: AsyncSession,
    spec: RecordType,
    plan: Plan,
    inserted: set[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    # One identity-merged event per row, ever: a row linked again (or a link inserted again
    # after a downgrade with -x allow-identity-loss=1 and an upgrade) finds the first one.
    olds = {old for old, _ in inserted}
    merged: set[uuid.UUID] = set()
    for chunk in chunked(sorted(olds)):
        merged.update(
            (
                await session.scalars(
                    select(RawHealthDataEvent.raw_id).where(
                        RawHealthDataEvent.event == MERGED_EVENT,
                        among(session, RawHealthDataEvent.raw_id, chunk),
                    )
                )
            ).all()
        )
    now = datetime.now(UTC)
    events = []
    for old, _, _ in plan.links:
        if old.raw_id in olds and old.raw_id not in merged:
            merged.add(old.raw_id)
            events.append(
                {
                    "raw_id": old.raw_id,
                    "data_type": spec.name,
                    "google_data_point_id": old.google_data_point_id,
                    "event": MERGED_EVENT,
                    "occurred_at": now,
                    "previous_deleted_at": None,
                }
            )
    for chunk in chunked(events):
        await session.execute(insert(RawHealthDataEvent), list(chunk))


async def _batch_chains(session: AsyncSession, data_type: str, planned: Chains) -> Chains:
    """The recorded chains now, plus a dry run's planned links, evidence and decisions."""
    chains = await load_chains(session, data_type)
    for link in planned.links - chains.links:
        chains.add(*link, decided_by=planned.decided_by.get(link, "reconciler"))
    for (left, right), items in planned.evidence.items():
        for item in items:
            chains.add_evidence(left, right, item.key, item.seen)
    for (first, second), decision in planned.chain_decisions.items():
        chains.decide(decision, first, second)
    for (first, second), decision in planned.pair_decisions.items():
        chains.decide(decision, first, second)
    chains.refute(planned.refuted_events)
    for fingerprint, span in planned.spans.items():
        if fingerprint not in chains.spans:
            chains.set_span(fingerprint, span)
    return chains


async def settle_starts(
    session: AsyncSession,
    spec: RecordType,
    *,
    dry_run: bool,
    count: bool,
    settlement: ChainSettlement,
    selection: SettlementRows,
) -> None:
    """Every row stored at one of ``starts``, plus the rows ``extra`` (by id: rows whose
    stored start is stale), settled together (keys of the extra rows only, then)."""
    found = await archive_reading.rows_at(session, spec, selection.starts)
    rows, mismatched = rows_of(spec, found, settlement.stats if count else None)
    rows.extend(mismatched)
    if selection.extra:
        by_id = (
            await session.execute(
                select(*COLUMNS).where(among(session, RawHealthData.id, sorted(selection.extra)))
            )
        ).tuples()
        stale, stale_mismatched = rows_of(spec, by_id.all(), None)
        keys = {row.identity.match for row in (*stale, *stale_mismatched)}
        known = {row.raw_id for row in rows}
        rows = [row for row in rows if row.identity.match in keys]
        rows.extend(row for row in (*stale, *stale_mismatched) if row.raw_id not in known)
    chains = await _batch_chains(session, spec.name, settlement.chains)
    plan = await plan_batch(session, spec, rows, chains, settlement.stats, count_groups=count)
    await _write_plan(session, spec, plan, settlement.stats, dry_run=dry_run)


async def link_pass(
    sessions: async_sessionmaker,
    spec: RecordType,
    *,
    exclusively: Exclusive,
    after: datetime | None,
    options: BackfillOptions,
    settlement: ChainSettlement,
) -> None:
    """Pass 2 over every row of the data type (from ``after``), batch by batch."""
    cursor: tuple[datetime, uuid.UUID] | None = None
    batches = 0
    while True:
        stats = BackfillStats(mode=settlement.stats.mode)
        analyze = not options.dry_run and ((batches & (batches - 1)) == 0 or batches % 50 == 0)
        batches += 1

        async def work(
            session: AsyncSession,
            stats: BackfillStats = stats,
            analyze: bool = analyze,
            cursor: tuple[datetime, uuid.UUID] | None = cursor,
        ) -> tuple[datetime, uuid.UUID] | None:
            if analyze:
                await database(session).control(session, text(f"ANALYZE {IDENTITY_TABLE}"))
            batch = await archive_reading.read_batch(
                session, spec.name, options.batch_size, cursor, after
            )
            if not batch:
                return None
            # Stale-start rows were noted by id in pass 1 (also those before ``after``) and
            # are settled last, with the rows of their parsed start.
            rows, _ = rows_of(spec, batch, stats)
            chains = await _batch_chains(session, spec.name, settlement.chains)
            plan = await plan_batch(session, spec, rows, chains, stats)
            await _write_plan(session, spec, plan, stats, dry_run=options.dry_run)
            return (batch[-1][3], batch[-1][0])

        reached = await exclusively(
            sessions,
            spec.name,
            work,
            options.statement_timeout,
            options.notice,
            dry_run=options.dry_run,
        )
        if reached is None:
            break
        cursor = reached
        settlement.stats.add(stats)
        if options.progress is not None:
            options.progress(spec.name, settlement.stats, as_utc(reached[0]))
