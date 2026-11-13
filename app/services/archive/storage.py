from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import cast as cast_type

from sqlalchemy import Table, Text, and_, case, cast, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ingestion_stats import StoreStats as StoreStats
from app.core.logging import event
from app.core.provenance import current_source, provenance
from app.core.records import RecordType
from app.db.dialect import database, dialect_insert
from app.google_health.capture import CapturedResponse
from app.google_health.client import Page
from app.models.identity_tables import RawHealthDataSupersession
from app.models.raw_versions import RawApiResponse
from app.models.state import RawHealthData
from app.parsers.common import payload_hash, temporal
from app.parsers.text import prepare_optional_text
from app.services.identities import source_chain_repository
from app.services.identities.hollow_names import link_hollow_rows
from app.services.identities.identity import resolve_ids
from app.services.identities.identity_common import Resolution, among
from app.services.identities.identity_writes import add_aliases, add_links
from app.services.identities.source_chain_repository import record_evidence, record_sightings

from .normalization import normalize
from .raw_rejections import reject_unstorable
from .storage_context import StoreProvenance, WriteStamp

logger = logging.getLogger(__name__)


@contextmanager
def recording_source(source: str) -> Iterator[None]:
    """The write path for records stored (and responses fetched) in the enclosed work."""
    with provenance(source=source):
        yield


async def insert_responses(
    session: AsyncSession, responses: Sequence[CapturedResponse]
) -> list[int]:
    """Append Google responses to raw_api_responses; returns their ids in order."""
    if not responses:
        return []
    table = cast_type(Table, RawApiResponse.__table__)
    statement = insert(table).returning(table.c.id, sort_by_parameter_order=True)
    result = await session.execute(statement, [response.row() for response in responses])
    return list(result.scalars().all())


async def store_page(
    session: AsyncSession,
    spec: RecordType,
    page: Page,
    *,
    source: str | None = None,
    fetch_started: datetime | None = None,
) -> StoreStats:
    """Store a fetched page: its Google responses and its records, in one transaction.

    ``fetch_started``: when the fetch (the run of pages) this page belongs to began."""
    return await store_records(
        session,
        spec,
        page.data_points,
        source=source,
        responses=page.responses,
        fetch_started=fetch_started,
    )


@dataclass(frozen=True, slots=True)
class PageRecord:
    record: object
    google_id: str
    digest: str
    start: datetime | None
    end: datetime | None
    timezone: str | None


class _Collision(Exception):
    """A row this page writes onto changed, or a row it creates was created, meanwhile."""


# Bound retries even when writers outside per-type run ownership keep revising a page.
# Resolution reads before the sorted row locks: after waiting for a writer to commit,
# a page may find those readings stale and must release its savepoint and resolve again.
# Sorted locks prevent deadlocks, but cannot prevent these optimistic collisions.
# With 32 overlapping writers, eight attempts can expire while other pages still finish.
# Allow two attempts per writer in that burst; this is a finite budget, not a guarantee
# against an unbounded stream of competing writes. No payload is accepted using stale
# identity state merely because the budget is running out.
WRITE_ATTEMPTS = 64
# Full jitter spreads retrying writers; its cap stays short even with the larger budget.
# Only collision retries wait, never the first attempt or a successful page.
FIRST_RETRY_WAIT = 0.05
MAX_RETRY_WAIT = 0.4


async def _wait_before_retry(attempt: int) -> None:
    """Spread collisions with a doubling, capped wait, including late retries.

    The larger retry budget does not increase the per-retry wait ceiling.
    """
    await asyncio.sleep(
        random.uniform(0, min(FIRST_RETRY_WAIT * 2 ** (attempt - 1), MAX_RETRY_WAIT))
    )


def _attempt_scope(session: AsyncSession):
    """Retry policy and attempt rollback scope supplied by the database adapter."""
    return database(session).attempt_scope(session, WRITE_ATTEMPTS)


async def store_records(
    session: AsyncSession,
    spec: RecordType,
    records: Sequence[object],
    *,
    source: str | None = None,
    responses: Sequence[CapturedResponse] = (),
    fetch_started: datetime | None = None,
) -> StoreStats:
    """Archive one page of records; a changed payload keeps the previous one as a version.

    ``raw_health_data`` is overwritten with the newest payload, and the trigger from
    migration 20261020_0011 copies the replaced row into ``raw_health_data_versions`` in
    the same statement. A record that appears more than once in ``records`` with
    different payloads is written once per distinct payload, in order, so each becomes
    its own version (consecutive identical copies collapse into one).

    ``responses`` are the HTTP exchanges the records were read from: they are appended to
    ``raw_api_responses`` first, in this same transaction, and each new payload is linked
    to the last of them (``response_id``), so a crash keeps both or neither.

    A record PostgreSQL jsonb cannot hold (U+0000, a lone surrogate, NaN/Infinity) is set
    apart first (app/services/archive/raw_rejections.py, ``StoreStats.rejected``), in this same
    transaction; it is neither stored nor sanitized, and the rest of the page is stored.

    Records without a Google id are written onto the row that holds their identity
    (app/services/identities/identity.py). The rows written onto are locked and checked against what
    the resolution read; new rows are created with ``ON CONFLICT DO NOTHING``. When another
    writer changed or created one of them meanwhile, the page is resolved again (inside a
    savepoint), so a payload is never written onto a row that holds another source.

    The time written to ``last_seen_at`` is read after the data type's lock was granted, and
    ``last_seen_at`` never moves backwards (``GREATEST``): a writer that waited for the lock
    cannot make a record look unseen to a fetch that started meanwhile.

    ``fetch_started``: when the fetch that returned these records began (its first page; a
    resumed fetch passes the original start). Evidence of two devices is noted with it, so the
    pages of one fetch count as one (app/services/identities/source_chains.py, ``established``);
    deletion marking reads it from the fetch itself.
    """
    origin = source or current_source()
    response_ids = await insert_responses(session, responses)
    response_id = response_ids[-1] if response_ids else None
    # When Google returned the records (the sources' sightings): the response's fetch time.
    fetched_at = responses[-1].fetched_at if responses else None
    records, rejected = await reject_unstorable(
        session, spec, records, source=origin, response_id=response_id
    )
    digests = [payload_hash(record) for record in records]
    attempts, attempt_scope = _attempt_scope(session)
    for attempt in range(attempts):
        if attempt:
            await _wait_before_retry(attempt)
        try:
            async with attempt_scope():
                stats = await _store(
                    session,
                    spec,
                    records,
                    digests,
                    provenance=StoreProvenance(origin, response_id, fetched_at, fetch_started),
                )
            stats.rejected = rejected
            return stats
        except _Collision:
            event(logger, "RECORD_BATCH_RESOLVED_AGAIN", data_type=spec.name)
    raise RuntimeError(
        f"{spec.name}: a concurrent writer kept changing the rows of this page; "
        "nothing of it was stored (the next sync fetches it again)"
    )


async def _lock_rows(
    session: AsyncSession, spec: RecordType, google_ids: list[str], resolution: Resolution
):
    """Lock the existing rows the page relies on (the rows it writes onto, every row that
    could hide one of them, the rows it links; key order, like the upserts) and
    check that each is still as it was resolved against - its payload, deletion mark,
    quarantine and links - so another page that revised, revived or linked one of them
    meanwhile makes this one resolve again. Returns google id -> (raw id, payload hash)."""
    table = RawHealthData
    found: dict[str, tuple] = {}
    current: dict = {}
    for offset in range(0, len(google_ids), 5000):
        query = (
            select(
                table.google_data_point_id,
                table.id,
                table.payload_hash,
                table.source_deleted_at,
                table.is_quarantined,
            )
            .where(
                table.data_type == spec.name,
                among(session, table.google_data_point_id, google_ids[offset : offset + 5000]),
            )
            .order_by(table.google_data_point_id)
            .with_for_update()
        )
        for google_id, raw_id, digest, deleted, quarantined in (
            await session.execute(query)
        ).tuples():
            found[google_id] = (raw_id, digest)
            current[raw_id] = (digest, deleted is not None, bool(quarantined))
    if any(current.get(raw_id) != state for raw_id, state in resolution.expected.items()):
        raise _Collision
    watched = sorted(resolution.expected_links, key=str)
    links: dict = {raw_id: set() for raw_id in watched}
    link = RawHealthDataSupersession
    for offset in range(0, len(watched), 5000):
        rows = await session.execute(
            select(link.old_raw_id, link.new_raw_id).where(
                among(session, link.old_raw_id, watched[offset : offset + 5000])
            )
        )
        for old, new in rows.tuples():
            links[old].add(new)
    if any(
        links[raw_id] != set(expected) for raw_id, expected in resolution.expected_links.items()
    ):
        raise _Collision
    return found


async def _store(
    session: AsyncSession,
    spec: RecordType,
    records: Sequence[object],
    digests: Sequence[str],
    *,
    provenance: StoreProvenance,
) -> StoreStats:
    stats = StoreStats()
    resolution = await resolve_ids(session, spec, records, digests)
    by_id, duplicates = _page_sequences(spec, records, resolution.ids, digests)

    # The rows written onto and the rows new ones are linked to (resolution.locked).
    google_ids = sorted(set(by_id) | resolution.locked)
    existing = await _lock_rows(session, spec, google_ids, resolution)
    # After the data type's lock and the row locks: a writer that waited never stamps an
    # older time than a fetch that began while it waited.
    now = datetime.now(UTC)
    existing_hashes = {google_id: digest for google_id, (_, digest) in existing.items()}
    raw_ids = {google_id: raw_id for google_id, (raw_id, _) in existing.items()}
    created = sorted(google_id for google_id in by_id if google_id in resolution.created)
    if any(google_id in existing_hashes for google_id in created):
        raise _Collision
    if created:
        rows = [
            _raw_row(spec, by_id[google_id][0], now, provenance.origin, provenance.response_id)
            for google_id in created
        ]
        inserted = await _insert_new(session, rows)
        if set(inserted) != set(created):
            raise _Collision
        raw_ids.update(inserted)

    rounds, normalized_values = _plan_writes(
        spec,
        by_id,
        created,
        existing_hashes,
        stats,
        stamp=WriteStamp(now, provenance.origin, provenance.response_id),
    )
    # Writers outside run ownership (scripts/audit_date_gaps.py next to a sync) lock rows in
    # key order, so two overlapping pages wait for each other instead of deadlocking.
    for rows in rounds:
        if rows:
            rows.sort(key=lambda row: row["google_data_point_id"])
            await _upsert_raw_many(session, rows)
    await add_aliases(session, spec.name, resolution.new_aliases, raw_ids)
    # New rows of a decided device that show what a counted row of it shows: linked at once,
    # so the record is counted once (readers skip a row superseded by a link in force).
    await add_links(session, spec.name, resolution.links, raw_ids, now)
    for model, values in normalized_values.items():
        values.sort(key=lambda row: (row.get("data_type", ""), row["google_data_point_id"]))
        await _upsert_normalized_many(session, model, values)
    # A row archived under a hollow name before: held by the row of its record, once that row
    # holds its payload (app/services/identities/hollow_names.py).
    await link_hollow_rows(session, spec, {key: seq[-1].digest for key, seq in by_id.items()}, now)
    await _record_source_activity(
        session, spec, resolution, provenance.fetched_at, provenance.fetch_started
    )
    if records:
        event(
            logger,
            "RECORD_BATCH_STORED",
            data_type=spec.name,
            received=len(records),
            inserted=stats.inserted,
            updated=stats.updated,
            unchanged=stats.unchanged,
            duplicates=duplicates,
            versions_kept=stats.versions_kept,
            chained=resolution.chained,
            kept_apart=resolution.kept_apart,
        )
    return stats


def _page_sequences(
    spec: RecordType, records: Sequence[object], ids: Sequence[str], digests: Sequence[str]
) -> tuple[dict[str, list[PageRecord]], int]:
    """Group consecutive distinct payloads per record, retaining their arrival order."""
    by_id: dict[str, list[PageRecord]] = {}
    duplicates = 0
    for record, google_id, digest in zip(records, ids, digests, strict=True):
        start = end = timezone = None
        if isinstance(record, dict):
            start, end, timezone = temporal(spec, record)
        sequence = by_id.setdefault(google_id, [])
        if sequence:
            duplicates += 1
            if sequence[-1].digest == digest:
                continue
            event(logger, "RECORD_BATCH_DUPLICATE_CONFLICT", data_type=spec.name)
        sequence.append(PageRecord(record, google_id, digest, start, end, timezone))

    return by_id, duplicates


def _plan_writes(
    spec: RecordType,
    by_id: dict[str, list[PageRecord]],
    created: list[str],
    existing_hashes: dict[str, str],
    stats: StoreStats,
    *,
    stamp: WriteStamp,
) -> tuple[list[list[dict]], dict[type, list[dict]]]:
    """Plan one payload per key per statement and normalize only the final payload."""
    # One INSERT ... ON CONFLICT statement may not touch the same row twice on
    # PostgreSQL, so the n-th payload of every record goes into the n-th statement (the
    # first payload of a new row is in already).
    rounds: list[list[dict]] = []
    normalized_values: dict[type, list[dict]] = defaultdict(list)
    new_rows = set(created)
    for google_id, sequence in by_id.items():
        for position, item in enumerate(sequence):
            if position == 0 and google_id in new_rows:
                continue
            while len(rounds) <= position:
                rounds.append([])
            rounds[position].append(
                _raw_row(spec, item, stamp.now, stamp.origin, stamp.response_id)
            )
        existing_hash = existing_hashes.get(google_id)
        hashes = [existing_hash, *(item.digest for item in sequence)]
        replaced = sum(old is not None and old != new for old, new in pairwise(hashes))
        stats.versions_kept += replaced
        if existing_hash is None:
            stats.inserted += 1
        elif replaced:
            stats.updated += 1
        else:
            stats.unchanged += 1
        normalized = normalize(spec, sequence[-1].record)
        if normalized is None:
            stats.normalization_skipped += 1
        elif existing_hash is None or replaced:
            # Keyed like its raw row, which may be an older id of the same record.
            values = {**normalized.values, "google_data_point_id": google_id}
            normalized_values[normalized.model].append(values)
            stats.normalized += 1
    return rounds, normalized_values


async def _record_source_activity(
    session: AsyncSession,
    spec: RecordType,
    resolution: Resolution,
    fetched_at: datetime | None,
    fetch_started: datetime | None,
) -> None:
    if resolution.state is None:
        return
    # Late in the transaction: the sightings row of a source is the one row every writer
    # of the data type shares, so it is held for as short as possible.
    seen_at = fetched_at or await source_chain_repository.database_now(session)
    fetch = fetch_started or seen_at
    await record_evidence(
        session,
        spec.name,
        resolution.state.chains,
        [(*found, fetch) for found in resolution.evidence],
    )
    await record_sightings(
        session,
        spec.name,
        resolution.seen,
        seen_at,
        history_known=resolution.state.history_known,
    )


async def _insert_new(session: AsyncSession, rows: list[dict]) -> dict[str, object]:
    """Insert new rows; google id -> raw id of those inserted (one another writer created
    first is not)."""
    table = RawHealthData.__table__
    # One cached statement, many parameter sets (batched into multi-row VALUES by the driver
    # layer): compiling a 1,000-row VALUES list per page costs more than the insert.
    statement = (
        dialect_insert(session, table)
        .on_conflict_do_nothing(index_elements=["data_type", "google_data_point_id"])
        .returning(table.c.google_data_point_id, table.c.id)
    )
    result = await session.execute(statement, rows)
    return dict(result.tuples().all())


def _raw_row(
    spec: RecordType, item: PageRecord, now: datetime, origin: str, response_id: int | None
) -> dict:
    # Validate the optional source index, leaving the received JSON and identity intact.
    timezone_values = {"source_timezone": item.timezone}
    prepare_optional_text(RawHealthData, timezone_values)
    return {
        "google_data_point_id": item.google_id,
        "data_type": spec.name,
        "source_start_time": item.start,
        "source_end_time": item.end,
        **timezone_values,
        "raw_json": item.record,
        "payload_hash": item.digest,
        "first_seen_at": now,
        "last_seen_at": now,
        "source_deleted_at": None,
        "payload_received_at": now,
        "payload_source": origin,
        "response_id": response_id,
    }


async def _upsert_normalized_many(session: AsyncSession, model, rows: list[dict]) -> None:
    if not rows:
        return
    table = model.__table__
    statement = dialect_insert(session, table)
    conflict_columns = ["google_data_point_id"]
    if "data_type" in rows[0]:
        conflict_columns.insert(0, "data_type")
    updates = {key: getattr(statement.excluded, key) for key in rows[0]}
    updates["updated_at"] = datetime.now(UTC)
    statement = statement.on_conflict_do_update(
        index_elements=conflict_columns,
        set_=updates,
    )
    await session.execute(statement, rows)


async def _upsert_raw_many(session: AsyncSession, rows: list[dict]) -> None:
    if not rows:
        return
    table = RawHealthData.__table__
    statement = dialect_insert(session, table)
    highest = database(session).greatest
    # The payload is overwritten in place; when its hash or stored text changes, the
    # trigger of migration 20261020_0011 has already copied the replaced row into
    # raw_health_data_versions. When and how a payload arrived only changes with it.
    unchanged = and_(
        table.c.payload_hash == statement.excluded.payload_hash,
        cast(table.c.raw_json, Text) == cast(statement.excluded.raw_json, Text),
    )
    updates = {
        "source_start_time": statement.excluded.source_start_time,
        "source_end_time": statement.excluded.source_end_time,
        "source_timezone": statement.excluded.source_timezone,
        "raw_json": statement.excluded.raw_json,
        "payload_hash": statement.excluded.payload_hash,
        # Never backwards: a writer that waited must not make the record look older.
        "last_seen_at": highest(table.c.last_seen_at, statement.excluded.last_seen_at),
        "source_deleted_at": None,
        "payload_received_at": case(
            (unchanged, table.c.payload_received_at),
            else_=statement.excluded.payload_received_at,
        ),
        "payload_source": case(
            (unchanged, table.c.payload_source), else_=statement.excluded.payload_source
        ),
        "response_id": case((unchanged, table.c.response_id), else_=statement.excluded.response_id),
        "updated_at": datetime.now(UTC),
    }
    statement = statement.on_conflict_do_update(
        index_elements=["data_type", "google_data_point_id"],
        set_=updates,
    )
    await session.execute(statement, rows)
