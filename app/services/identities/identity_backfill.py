"""The identity reconciler (``healthos identity-backfill``): chains, aliases and links.

The write path (app/services/identities/identity.py) never decides that two source fingerprints are
one
device; it stores an unknown fingerprint's records on rows of their own. This module (and the
owner's ``identity-decide``, app/services/identities/identity_decisions.py) decides, and links what
was
decided. Per data type:

1. **Chains.** A *full* run first reads every row without a Google id and every earlier
   version (``raw_health_data`` is only read): each gives a sighting of its source
   fingerprint (received .. last seen), and two rows of one key that one page stored or last
   saw together, with two different readings, are evidence of two devices
   (``raw_source_pairs``). The spans are merged into ``raw_source_sightings`` (complete from
   now on). An *incremental* run (nightly) uses the sightings and evidence as the write path
   keeps them. Either way links whose two fingerprints became established distinct get a
   ``refuted`` event, then the chains are decided per stable class by the rule of
   app/services/identities/source_chains.py and appended to ``raw_source_chains``, and the devices
   that
   changed are logged (``raw_source_devices``, what readers count by); the cases the rule
   leaves open are listed in the report (``ambiguous``), every chain not yet reviewed by the
   owner under ``review``, pairs that look like a rename although evidence made them two
   devices under ``review_distinct``.
2. **Links.** Per record (match key: the v2 key without the optional device fields, so a form with
   ``formFactor`` added is compared with the form without), the rows are grouped by device (one
   fingerprint, or several joined by live chain links). Rows superseded by a link in force are
   settled; a row whose links are no longer in force (its chain was rejected or refuted) is
   settled again like any other. Within one device the row that holds the record (a live row
   first, one that already holds others first, then the newest) keeps it, and every other row of
   that device is linked to it in ``raw_health_data_supersessions`` with the reason and both
   fingerprints, plus an ``identity-merged`` event (once per row) - a row of *another
   fingerprint* of the device only when it measured the same (``reading``): a value of its own
   is never hidden (``kept_differing``). The rows a linked row held are linked to the holder
   too, so two devices joined later end with one holder. Rows of different devices are never
   linked, nor is a live row linked to a deleted one or a link made that would close a cycle
   (``conflicts``: both rows count, listed in status). Every row whose own id does not name its
   key and fingerprint gets an alias (``synthetic:v2s:`` rows need none). A deleted-marked row
   with no live row of its device stays as it is: a genuine deletion (``deletions_kept``), or a
   *suspicious* one when another kindred fingerprint not established distinct holds the
   record live (``deletions_suspicious``: possibly a rename nobody decided, listed for review).

A full run reads all rows in keyset order of ``(source_start_time, id)`` (index
``ix_raw_health_data_type_start``): every row of a v2 key has the same start, and a batch
always ends after the last row of its start time, so a key is never split. Rows whose stored
``source_start_time`` differs from the parsed start (an older parser) are noted by id during
the first pass and settled last with the rows of their parsed start - also when ``--after``
skips the rest of the second pass. An incremental run reads only the start times of rows
created or given a new payload (``updated_at`` and ``payload_received_at``) since its checkpoint
minus ``OVERLAP``, and, when it decided a new link, since the new fingerprint's first sighting;
they are streamed, never held in memory at once. A data type that never had a full run gets
its first full pass from the incremental run, and a data type that fails is reported while
the others still run.

Each batch of the second pass is one transaction holding the data type's exclusive lock
(writers of that data type wait for it, then continue): it tries for it without queueing for
``LOCK_TIMEOUT`` and then waits in the queue at most ``QUEUE_TIMEOUT``, and once it holds it
waits at most ``ROW_LOCK_TIMEOUT`` for any row lock of another transaction; then it backs off
and retries, so it never queues every writer behind a long sync or import. A dry run takes
no lock at all and writes nothing. Inserts are ``ON CONFLICT DO NOTHING`` in chunks (far below
PostgreSQL's 32,767 parameters), so a run is idempotent and can be stopped and run again at
any time. Only one run at a time: a second one refuses at once.

Run orchestration and transaction retries stay at this entry point.
Timeline/chain reconciliation, link planning/writing, agreement and checkpoint bookkeeping
live in the corresponding identity_backfill_* modules, which never import this executor.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import error_diagnostics, error_text, event
from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.db.dialect import database
from app.db.postgres import LOCK_NOT_AVAILABLE, sqlstate
from app.models.state import RawHealthData

from . import identity_backfill_chains as chains
from . import identity_backfill_linking as linking
from . import identity_backfill_reading as archive_reading
from . import source_chain_repository
from .identity_backfill_agreement import (
    agreement_of,
    count_shared,
    mark_counted,
    review_agreeing,
)
from .identity_backfill_planning import ChainSettlement, SettlementRows
from .identity_backfill_reading import spec_for
from .identity_backfill_state import (
    OVERLAP,
    DeviceChange,
    Notice,
    ReconcileResult,
    prepare,
    reconcile_state,
    save_state,
    settled,
)
from .identity_backfill_stats import BackfillStats
from .identity_options import DEFAULT_BACKFILL_OPTIONS, BackfillOptions
from .identity_reconcile_window import (
    LOCK_ERROR,
    current_deadline,
    keep_trying,
    waiting_until,
)
from .source_chain_locks import LOCK_TIMEOUT, LockBusy, backfill_lock, lock_data_type
from .source_chain_model import Chains

logger = logging.getLogger(__name__)

# A busy batch retries with bounded exponential backoff.
LOCK_ATTEMPTS = 8


class LockUnavailable(RuntimeError):
    """A batch could not get its data type's exclusive lock."""


def failure_summary(exc: BaseException) -> str:
    """What a failed data type keeps in its result and ``identity_reconcile_state``.

    ``LockUnavailable`` is written here from the data type's name and the attempts alone, and
    ``status`` and ``/health`` find its ``LOCK_ERROR``; any other failure keeps only its class
    chain and SQLSTATE (``error_text``), since its message can quote source metadata."""
    if isinstance(exc, LockUnavailable):
        return str(exc)[:2000]
    return error_text(exc)


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _exclusively[T](
    sessions: async_sessionmaker,
    data_type: str,
    work: Callable[[AsyncSession], Awaitable[T]],
    statement_timeout: str,
    notice: Notice | None = None,
    *,
    dry_run: bool = False,
) -> T:
    """Run ``work`` in one transaction that holds ``data_type``'s exclusive lock.

    The lock is tried for without queueing for at most ``LOCK_TIMEOUT`` (writers never wait
    behind the request), then waited for in the queue at most ``QUEUE_TIMEOUT``; once held, any
    row lock is waited for at most ``ROW_LOCK_TIMEOUT``; then the transaction is rolled back
    and retried after a pause (``notice`` is told), ``LOCK_ATTEMPTS`` times or, in a run's
    second round, until its deadline (``waiting_until``). A dry run writes
    nothing and takes no lock: it reads what is committed."""
    if dry_run:
        async with sessions.begin() as session:
            await prepare(session, statement_timeout)
            return await work(session)
    attempt = 0
    while keep_trying(attempt, LOCK_ATTEMPTS):
        try:
            async with sessions.begin() as session:
                await lock_data_type(session, data_type, exclusive=True, wait=LOCK_TIMEOUT)
                await prepare(session, statement_timeout)
                return await work(session)
        except (DBAPIError, LockBusy) as exc:
            if isinstance(exc, DBAPIError) and sqlstate(exc) != LOCK_NOT_AVAILABLE:
                raise
            delay = min(60, 2 ** (attempt + 1))
            event(
                logger,
                "IDENTITY_RECONCILE_LOCK_BUSY",
                data_type=data_type,
                attempt=attempt + 1,
                retry_in_seconds=delay,
            )
            if notice is not None:
                deadline = current_deadline()
                limit = f"until {deadline:%H:%M} UTC" if deadline else f"of {LOCK_ATTEMPTS}"
                notice(
                    f"{data_type}: another transaction holds the identity lock or a row this "
                    f"batch needs; nothing was written, retrying in {delay} s "
                    f"(attempt {attempt + 1} {limit})"
                )
            await _pause(delay)
            attempt += 1
    deadline = current_deadline()
    waited = f", until {deadline:%H:%M} UTC" if deadline is not None else ""
    raise LockUnavailable(
        f"{data_type}: {LOCK_ERROR} (or a row it needs) in "
        f"{attempt} attempts{waited} (a long sync, deletion check or import of this data type "
        "held it); nothing of this batch was written. Run again later: an incremental run (or "
        "the next night's) settles everything since its checkpoint again and any re-settlement "
        "a run or decision still owes (identity_reconcile_state.pending_settle); a full run "
        "starts its passes over (--after skips the second pass up to a start time)"
    )


async def backfill_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    after: datetime | None = None,
    options: BackfillOptions = DEFAULT_BACKFILL_OPTIONS,
) -> BackfillStats:
    """A full run for one data type (the whole archive; ``after`` skips the second pass
    up to a start time)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="full")
    async with sessions() as session:
        started = await source_chain_repository.database_now(session)
    timeline = await chains.timeline(sessions, spec, options.batch_size, options.statement_timeout)
    planned, _ = await chains.settle_chains(
        sessions,
        spec,
        timeline,
        total,
        exclusively=_exclusively,
        options=options,
    )
    await linking.link_pass(
        sessions,
        spec,
        after=after,
        exclusively=_exclusively,
        options=options,
        settlement=ChainSettlement(planned, total),
    )
    await _settle_stale_starts(
        sessions, spec, timeline.stale, options, ChainSettlement(planned, total)
    )
    async with sessions() as session:
        undated = await session.scalar(
            select(func.count()).where(
                RawHealthData.data_type == data_type, RawHealthData.source_start_time.is_(None)
            )
        )
    total.unidentified += undated or 0
    if options.dry_run:
        summary = await agreement_of(sessions, data_type)
    else:
        # Pass 2 counted every key of the pairs with evidence when it began (unless --after
        # skipped some), and settled every row (so no re-settlement is owed).
        summary = await mark_counted(
            sessions, data_type, set(planned.evidence) if after is None else set(), recount=True
        )
        if after is None:
            await settled(sessions, data_type)
    review_agreeing(planned, total, summary)
    if not options.dry_run:
        await save_state(sessions, data_type, ReconcileResult(started, total))
    return total


async def _settle_stale_starts(
    sessions: async_sessionmaker,
    spec: RecordType,
    stale: Sequence[tuple[datetime, UUID]],
    options: BackfillOptions,
    settlement: ChainSettlement,
) -> None:
    stale = sorted(stale)
    settlement.stats.start_mismatches = len(stale)
    for offset in range(0, len(stale), options.batch_size):
        chunk = stale[offset : offset + options.batch_size]
        stats = BackfillStats(mode="full")

        async def settle(
            session: AsyncSession,
            chunk: Sequence[tuple[datetime, UUID]] = chunk,
            stats: BackfillStats = stats,
        ) -> None:
            await linking.settle_starts(
                session,
                spec,
                dry_run=options.dry_run,
                count=False,
                settlement=ChainSettlement(settlement.chains, stats),
                selection=SettlementRows(
                    [start for start, _ in chunk], [raw_id for _, raw_id in chunk]
                ),
            )

        await _exclusively(
            sessions,
            spec.name,
            settle,
            options.statement_timeout,
            options.notice,
            dry_run=options.dry_run,
        )
        settlement.stats.add(stats)


async def relink_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    apply: Callable[[AsyncSession, Chains], Awaitable[None]],
    *,
    options: BackfillOptions = DEFAULT_BACKFILL_OPTIONS,
) -> BackfillStats:
    """Apply an owner decision (``apply``, run under the exclusive lock first), decide the
    chains again from the sightings, then settle again exactly the rows whose devices changed:
    the rows of every link between two fingerprints that are no longer one device (each side
    then counts once), and, for two fingerprints that newly are, every row written since the
    later of them was first seen (``_settle_changed``)."""
    spec = spec_for(data_type)
    total = BackfillStats(mode="decision")
    async with sessions() as session:
        started = await source_chain_repository.database_now(session)
    planned, change = await chains.settle_chains(
        sessions,
        spec,
        None,
        total,
        exclusively=chains.ChainExecution(_exclusively, apply),
        options=options,
    )
    await _settle_changed(
        sessions,
        spec,
        change,
        since=None,
        options=options,
        settlement=ChainSettlement(planned, total),
    )
    if not options.dry_run:
        await settled(sessions, data_type)
    review_agreeing(planned, total, await agreement_of(sessions, data_type))
    if not options.dry_run:
        await save_state(sessions, data_type, ReconcileResult(started, total, checkpoint=False))
    return total


async def reconcile_data_type(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    options: BackfillOptions = DEFAULT_BACKFILL_OPTIONS,
) -> BackfillStats:
    """An incremental run for one data type: chains from the sightings as they are, links
    for the rows changed since the checkpoint (minus ``OVERLAP``) and for every row whose
    device changed (``_settle_changed``). A data type that never had a full run gets its first
    full pass instead (``skipped`` says so), so none is left out for good."""
    spec = spec_for(data_type)
    state = await reconcile_state(sessions, data_type)
    if state is None or state.full_run_at is None or state.checkpoint_at is None:
        total = await backfill_data_type(
            sessions,
            data_type,
            options=options,
        )
        total.skipped = "no full identity-backfill of this data type yet: ran its first full pass"
        return total
    total = BackfillStats(mode="incremental")
    async with sessions() as session:
        started = await source_chain_repository.database_now(session)
    if not options.dry_run:
        await count_shared(
            sessions,
            spec,
            batch_size=options.batch_size,
            statement_timeout=options.statement_timeout,
        )
    planned, change = await chains.settle_chains(
        sessions,
        spec,
        None,
        total,
        exclusively=_exclusively,
        options=options,
    )
    await _settle_changed(
        sessions,
        spec,
        change,
        since=as_utc(state.checkpoint_at) - OVERLAP,
        options=options,
        settlement=ChainSettlement(planned, total),
    )
    if not options.dry_run:
        await settled(sessions, data_type)
    review_agreeing(planned, total, await agreement_of(sessions, data_type))
    if not options.dry_run:
        await save_state(sessions, data_type, ReconcileResult(started, total))
    return total


async def _settle_changed(
    sessions: async_sessionmaker,
    spec: RecordType,
    change: DeviceChange,
    *,
    since: datetime | None,
    options: BackfillOptions,
    settlement: ChainSettlement,
) -> None:
    """Settle again, batch by batch, the start times of

    * rows created or given a new payload since ``since`` (an incremental run), and rows whose
      deletion mark was cleared since then;
    * every row of a link between two fingerprints that are no longer one device (``lapsed``:
      a rejected or refuted chain, a ``distinct`` decision), whatever its age - its two sides
      are linked again within each device, so neither counts twice;
    * every row first seen since the later of two fingerprints that are newly one device was
      (``joined``): each of its rows was, also one stored before migration 20261020_0011
      (without ``payload_received_at``).

    The start times are read first, each set in one short transaction (none is held open
    while they are settled), and kept as integers."""
    touched: datetime | None = None
    for left, right in change.joined:
        spans = [settlement.chains.spans.get(left), settlement.chains.spans.get(right)]
        if spans[0] is None or spans[1] is None:
            touched = datetime(1970, 1, 1, tzinfo=UTC)
            continue
        later = max(span.first for span in spans if span is not None) - OVERLAP
        touched = later if touched is None else min(touched, later)
    starts = await archive_reading.collect_starts(
        sessions, spec.name, since=since, touched=touched, lapsed=change.lapsed
    )
    step = max(1, options.batch_size // 10)
    for offset in range(0, len(starts), step):
        chunk = [archive_reading.from_micros(value) for value in starts[offset : offset + step]]
        stats = BackfillStats(mode=settlement.stats.mode)

        async def settle(
            session: AsyncSession,
            chunk: Sequence[datetime] = chunk,
            stats: BackfillStats = stats,
        ) -> None:
            await linking.settle_starts(
                session,
                spec,
                dry_run=options.dry_run,
                count=True,
                settlement=ChainSettlement(settlement.chains, stats),
                selection=SettlementRows(chunk, ()),
            )

        await _exclusively(
            sessions,
            spec.name,
            settle,
            options.statement_timeout,
            options.notice,
            dry_run=options.dry_run,
        )
        settlement.stats.add(stats)
        if options.progress is not None:
            options.progress(spec.name, settlement.stats, chunk[-1])


async def data_types_in_archive(sessions: async_sessionmaker) -> list[str]:
    async with sessions() as session:
        return sorted((await session.scalars(select(RawHealthData.data_type).distinct())).all())


async def identity_backfill(
    sessions: async_sessionmaker,
    *,
    data_types: Sequence[str] | None = None,
    after: datetime | None = None,
    incremental: bool = False,
    lock_deadline: datetime | None = None,
    options: BackfillOptions = DEFAULT_BACKFILL_OPTIONS,
) -> dict[str, BackfillStats]:
    """Raises ``BackfillRunning`` when another run holds the single-run lock.

    ``incremental``: only what changed since each data type's checkpoint (the nightly run);
    a data type that never had a full run gets its first full pass (``skipped`` says so).
    A data type that fails (its lock stays busy, a statement times out) is reported with its
    ``error`` and saved in ``identity_reconcile_state``; the run goes on with the others.
    ``lock_deadline``: a data type whose lock stayed busy (a long sync of it) is tried again
    after all the others, its batches retrying until then, before it counts as failed."""
    mode = "incremental" if incremental else "full"
    async with backfill_lock(sessions, dry_run=options.dry_run):
        names = list(data_types) if data_types else await data_types_in_archive(sessions)
        results: dict[str, BackfillStats] = {}

        async def run_one(name: str, *, defer: bool) -> None:
            async with sessions() as session:
                started = await source_chain_repository.database_now(session)
            try:
                if incremental:
                    results[name] = await reconcile_data_type(
                        sessions,
                        name,
                        options=options,
                    )
                else:
                    results[name] = await backfill_data_type(
                        sessions,
                        name,
                        after=after,
                        options=options,
                    )
            except Exception as exc:
                if defer and isinstance(exc, LockUnavailable):
                    # Tried again after the others, until the deadline.
                    event(logger, "IDENTITY_RECONCILE_DATA_TYPE_DEFERRED", data_type=name)
                    return
                # Source and device metadata reach SQL parameters and database errors: the
                # log, the saved state and the run's result keep value-free diagnostics.
                logger.error(
                    "Identity reconciliation of a data type failed",
                    extra={
                        "event": "IDENTITY_RECONCILE_DATA_TYPE_FAILED",
                        "data_type": name,
                        "error_type": type(exc).__name__,
                        **error_diagnostics(exc, frames=True),
                    },
                )
                error = failure_summary(exc)
                if not options.dry_run:
                    await save_state(
                        sessions, name, ReconcileResult(started, None, error=error, mode=mode)
                    )
                results[name] = BackfillStats(mode=mode, error=error)

        for name in names:
            await run_one(name, defer=lock_deadline is not None)
        with waiting_until(lock_deadline):
            for name in [name for name in names if name not in results]:
                await run_one(name, defer=False)
        if not options.dry_run:
            await analyze_identity_tables(sessions)
    return {name: results[name] for name in names}


async def analyze_identity_tables(sessions: async_sessionmaker) -> None:
    async with sessions.begin() as session:
        # Fresh statistics for the lookups every stored page makes from now on.
        await database(session).control(
            session,
            text(
                "ANALYZE raw_health_data_identity, raw_health_data_supersessions, "
                "raw_source_chains, raw_source_sightings, raw_source_devices"
            ),
        )
