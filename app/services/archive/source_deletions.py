"""Mark archived records that a complete re-fetch of their window no longer returns.

Google Health has no tombstones in list responses: a deleted sleep or exercise record
simply stops appearing. After a window has been fetched from its first page to its last,
any archived record that the same filter must have matched, but which was not seen during
that fetch, is marked with ``source_deleted_at``. Nothing is deleted or overwritten; if
the record comes back, ``store_records`` clears the mark again.

"Not seen" is decided per record identity, not per row (app.services.identities.identity.
present_records). A row is not marked while, in the same fetch, a row with its v2 key *and the
same reading* was seen

* that it is linked with (the row holding its record, or another duplicate of it), or of the
  *same device* - the same source fingerprint, or one joined to it by a usable chain
  (app/services/identities/source_chains.py): the record under another id; or
* of a fingerprint not *established distinct* from the row's (never returned with another
  value for one key): possibly the same device renamed, which only the reconciler or the
  owner decides. The mark is deferred, never guessed.

A deferred row is kept in ``identity_deferred_deletions`` until a later complete fetch of a
window that holds it decides it, whatever other windows are fetched meanwhile (or, once its two
sources are one device, the next complete fetch of the data type).

A row of a fingerprint established distinct never keeps a candidate: of two devices that
recorded at one instant, one can be deleted alone. Nor does a row of the device with another
value: the candidate then holds a value Google no longer returns.

Superseded rows are candidates too: their holder stands for them while it is live, but a row
that counts again because its holder was marked (or would) must be marked when its record is
gone. A superseded row whose holder this fetch saw, unchanged since they were linked with the
same reading, is the holder's record and is not read again (that would re-read the raw JSON of
every superseded row on every complete fetch). Candidates are read, decided and marked in
chunks, so a long window never sits in memory at once; a row another transaction has locked
(a page writing it: it is being seen) is skipped rather than waited for, so this check never
waits for a writer, and a writer never waits for it in a circle; a deferral of a skipped row
stays open for the next complete fetch.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.core.logging import event
from app.core.timeutil import as_utc
from app.db.dialect import dialect_insert
from app.google_health.data_types import (
    CIVIL_AFTER,
    CIVIL_BEFORE,
    AccessMethod,
    DataTypeSpec,
    RecordKind,
    query_time,
)
from app.models.identity import IdentityDeferredDeletion as Deferral
from app.models.identity import RawHealthDataSupersession, in_force
from app.models.state import RawHealthData
from app.services.identities.deferred_deletions import deferred_deletions as deferred_deletions
from app.services.identities.identity import among, present_records
from app.services.identities.identity_schema import identity_tables_missing
from app.services.identities.source_chains import (
    Chains,
    load_chains,
    lock_data_type,
    try_lock_deferrals,
)

logger = logging.getLogger(__name__)

# Candidates read and decided at a time.
CHUNK = 2000


@dataclass(frozen=True, slots=True)
class DeletionWindow:
    column: InstrumentedAttribute
    window: tuple[datetime, datetime]
    now: datetime


async def _note_deferred(
    session: AsyncSession,
    data_type: str,
    deferred: dict,
    skipped: set | frozenset = frozenset(),
    chains: Chains | None = None,
    *,
    scope: DeletionWindow,
) -> None:
    """Keep what this complete fetch deferred, row by row (``identity_deferred_deletions``,
    shown by ``healthos status`` and every reconciler run), and resolve every open
    deferral of a row in its window that it did not defer again (marked, kept, or returned):
    a later fetch of another window never hides what an earlier one deferred. A row it
    ``skipped`` (a writer held it) was not decided: its deferral stays open. Open deferrals
    of the data type whose two sources are one device now (``chains``: linked since, by the
    owner or the reconciler) are resolved too, whatever their window: the row's device returns
    its reading, so nothing is left to decide.

    The rows are written only under ``try_lock_deferrals``: when another complete fetch of the
    data type is writing them right now, nothing is waited for (a convoy of fetches behind one
    slow or killed transaction) and nothing is written; the next complete fetch of the window
    records and resolves them."""
    if await identity_tables_missing(session):
        return
    if not await try_lock_deferrals(session, data_type):
        event(
            logger,
            "SOURCE_DELETIONS_DEFERRALS_BUSY",
            data_type=data_type,
            count=len(deferred),
        )
        return
    if deferred:
        table = Deferral.__table__
        statement = dialect_insert(session, table)
        statement = statement.on_conflict_do_update(
            index_elements=["raw_id"],
            set_={
                "fingerprint": statement.excluded.fingerprint,
                "returned_by": statement.excluded.returned_by,
                "last_deferred_at": statement.excluded.last_deferred_at,
                "resolved_at": None,
            },
        )
        values = [
            {
                "raw_id": raw_id,
                "data_type": data_type,
                "fingerprint": fingerprint,
                "returned_by": other,
                "first_deferred_at": scope.now,
                "last_deferred_at": scope.now,
                "resolved_at": None,
            }
            for raw_id, (fingerprint, other) in sorted(
                deferred.items(), key=lambda item: str(item[0])
            )
        ]
        for offset in range(0, len(values), 1000):
            await session.execute(statement, values[offset : offset + 1000])
    await _resolve_window_deferrals(
        session, data_type, set(deferred) | set(skipped), scope.column, scope.window, scope.now
    )
    if chains is not None:
        await _resolve_joined_deferrals(session, data_type, chains, scope.now)


async def _resolve_window_deferrals(
    session: AsyncSession,
    data_type: str,
    undecided: set,
    column: InstrumentedAttribute,
    window: tuple[datetime, datetime],
    now: datetime,
) -> None:
    lower, upper = window
    # The open deferrals of the data type (few), then their rows looked up by primary key for
    # each chunk and placed in the window here, never a join of the window with the deferrals:
    # the planner estimated both at one row and rescanned the whole window for every open
    # deferral (seconds to a minute, with the sync state locked).
    open_ids = sorted(
        raw_id
        for raw_id in (
            await session.scalars(
                select(Deferral.raw_id).where(
                    Deferral.data_type == data_type, Deferral.resolved_at.is_(None)
                )
            )
        ).all()
        if raw_id not in undecided
    )
    for offset in range(0, len(open_ids), CHUNK):
        rows = await session.execute(
            select(RawHealthData.id, RawHealthData.data_type, column).where(
                among(session, RawHealthData.id, open_ids[offset : offset + CHUNK])
            )
        )
        decided = [
            raw_id
            for raw_id, name, value in rows.tuples()
            if name == data_type and value is not None and lower <= as_utc(value) < upper
        ]
        if decided:
            await session.execute(
                update(Deferral)
                .where(
                    Deferral.data_type == data_type,
                    Deferral.resolved_at.is_(None),
                    among(session, Deferral.raw_id, decided),
                )
                .values(resolved_at=now)
                .execution_options(synchronize_session=False)
            )


def deletion_scope(
    spec: DataTypeSpec, start: datetime, end: datetime
) -> tuple[InstrumentedAttribute, datetime, datetime] | None:
    """Return (stored column, lower, upper) that the list filter for the window covers.

    Mirrors ``DataTypeSpec.filter_for`` on the bounds the client actually sends
    (``query_time``: UTC, whole seconds), never on finer caller datetimes. ``None``
    means deletions cannot be inferred: unfiltered catalogs, and rollup/reconcile
    responses, which are derived aggregates rather than individually deletable source
    records.
    """
    if not spec.time_filtered or spec.access_method != AccessMethod.list:
        return None
    start, end = query_time(start), query_time(end, upper=True)
    if spec.kind == RecordKind.daily:
        start_date = date.fromisoformat(start.isoformat()[:10])
        end_date = date.fromisoformat(end.isoformat()[:10])
        if end_date <= start_date or end.isoformat()[11:19] != "00:00:00":
            end_date += timedelta(days=1)
        lower = datetime.combine(start_date, time.min, UTC)
        upper = datetime.combine(end_date, time.min, UTC)
        return RawHealthData.source_start_time, lower, upper
    if spec.name == "sleep":
        return RawHealthData.source_end_time, start, end
    if spec.kind == RecordKind.session and spec.name != "electrocardiogram":
        # Civil start times: only records this far inside are certain to have matched.
        lower, upper = start + CIVIL_BEFORE, end - CIVIL_AFTER
        if lower >= upper:
            return None
        return RawHealthData.source_start_time, lower, upper
    # Samples, intervals, and electrocardiograms (whose filter has no upper bound,
    # so the returned set is a superset of this window).
    return RawHealthData.source_start_time, start, end


async def mark_source_deletions(
    session: AsyncSession,
    spec: DataTypeSpec,
    start: datetime,
    end: datetime,
    fetched_since: datetime,
) -> int:
    """Mark records in the window that were not seen since ``fetched_since``.

    ``store_records`` bumps ``last_seen_at`` for every returned record (changed or not),
    so a record in scope that is older than the start of the complete fetch was not
    returned by Google.
    """
    scope = deletion_scope(spec, start, end)
    if scope is None:
        return 0
    column, lower, upper = scope
    await lock_data_type(session, spec.name, exclusive=False)
    unseen = (
        RawHealthData.data_type == spec.name,
        RawHealthData.source_deleted_at.is_(None),
        RawHealthData.last_seen_at < fetched_since,
        column >= lower,
        column < upper,
    )
    marked = kept = deferred = 0
    # Raw id -> (its fingerprint, the source that returned its reading).
    deferrals: dict = {}
    # Rows a writer held: neither marked nor decided.
    skipped: set = set()
    now = datetime.now(UTC)
    cursor: tuple | None = None
    chains = await load_chains(session, spec.name)
    while True:
        query = select(RawHealthData.id, column).where(*unseen)
        if cursor is not None:
            query = query.where(tuple_(column, RawHealthData.id) > cursor)
        page = (
            (await session.execute(query.order_by(column, RawHealthData.id).limit(CHUNK)))
            .tuples()
            .all()
        )
        if not page:
            break
        cursor = (page[-1][1], page[-1][0])
        chunk = await _unheld_chunk(session, unseen, page, fetched_since)
        presence = await present_records(session, spec, chunk, fetched_since, chains)
        kept += len(presence.present)
        deferred += len(presence.deferred)
        deferrals.update(presence.deferred_by)
        spared = presence.present | presence.deferred
        ids = [raw_id for raw_id, _, _ in chunk if raw_id not in spared]
        marked += await _mark_unseen(session, unseen, ids, now, skipped)
        if len(page) < CHUNK:
            break
    if kept:
        event(logger, "SOURCE_DELETIONS_KEPT_BY_IDENTITY", data_type=spec.name, count=kept)
    if deferred:
        # Possibly renamed: the reconciler or the owner decides (identity-backfill, nightly;
        # identity-decide).
        event(logger, "SOURCE_DELETIONS_DEFERRED", data_type=spec.name, count=deferred)
    await _note_deferred(
        session,
        spec.name,
        deferrals,
        skipped,
        chains,
        scope=DeletionWindow(column=column, window=(lower, upper), now=now),
    )
    if marked:
        event(logger, "SOURCE_DELETIONS_MARKED", data_type=spec.name, count=marked)
    return marked


async def _unheld_chunk(
    session: AsyncSession,
    unseen: tuple,
    page: Sequence[tuple[UUID, datetime]],
    fetched_since: datetime,
):
    """Read JSON only for candidates without a live, unchanged holder in this fetch."""
    link, holder = aliased(RawHealthDataSupersession), aliased(RawHealthData)
    # Superseded by a link in force with the same reading, its holder seen by this fetch and
    # unchanged since: the holder's record, present. Looked up for each chunk's ids (by the
    # links' primary key), never as an anti-join over the window: the planner cannot estimate
    # the links in force, and a nested loop over every candidate and every holder grew with
    # the square of the window.
    held = select(link.old_raw_id).join(holder, holder.id == link.new_raw_id)
    held = held.where(
        link.same_reading.is_(True),
        holder.payload_hash == link.new_payload_hash,
        holder.last_seen_at >= fetched_since,
        holder.source_deleted_at.is_(None),
        in_force(link),
    )
    held_ids = set(
        (
            await session.scalars(
                held.where(among(session, link.old_raw_id, [raw_id for raw_id, _ in page]))
            )
        ).all()
    )
    # The rest's raw JSON, read now (none of a held row's).
    rest = [raw_id for raw_id, _ in page if raw_id not in held_ids]
    read = {}
    if rest:
        read = {
            raw_id: (raw_json, start)
            for raw_id, raw_json, start in (
                await session.execute(
                    select(
                        RawHealthData.id,
                        RawHealthData.raw_json,
                        RawHealthData.source_start_time,
                    ).where(*unseen, among(session, RawHealthData.id, rest))
                )
            ).tuples()
        }
    return [(raw_id, *read[raw_id]) for raw_id in rest if raw_id in read]


async def _mark_unseen(
    session: AsyncSession, unseen: tuple, ids: list, now: datetime, skipped: set
) -> int:
    """Recheck unseen state under nonblocking row locks; never wait for a writer."""
    if ids:
        # Rows a writer holds are being seen right now: skipped, never waited for.
        locked = list(
            (
                await session.scalars(
                    select(RawHealthData.id)
                    .where(*unseen, among(session, RawHealthData.id, ids))
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        skipped.update(set(ids) - set(locked))
        ids = locked
    if ids:
        result = await session.execute(
            update(RawHealthData)
            .where(*unseen, among(session, RawHealthData.id, ids))
            .values(source_deleted_at=now)
            .execution_options(synchronize_session=False)
        )
        # An UPDATE always yields a CursorResult; AsyncSession.execute is typed as Result.
        return cast(CursorResult[Any], result).rowcount or 0
    return 0


async def _resolve_joined_deferrals(
    session: AsyncSession, data_type: str, chains: Chains, now: datetime
) -> None:
    pairs = (
        await session.execute(
            select(Deferral.fingerprint, Deferral.returned_by)
            .where(Deferral.data_type == data_type, Deferral.resolved_at.is_(None))
            .distinct()
        )
    ).tuples()
    joined = sorted(pair for pair in pairs if chains.same_device(*pair))
    if joined:
        await session.execute(
            update(Deferral)
            .where(
                Deferral.data_type == data_type,
                Deferral.resolved_at.is_(None),
                tuple_(Deferral.fingerprint, Deferral.returned_by).in_(joined),
            )
            .values(resolved_at=now)
            .execution_options(synchronize_session=False)
        )
