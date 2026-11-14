"""Garmin write stages independent of pull scheduling and checkpoints."""

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Table, insert, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.fetch_window import FetchBudget, FetchWindow, PaginationPaused
from app.core.ingestion_stats import NormalizeStats, StoreStats
from app.core.jsonb import jsonb_storable
from app.core.logging import event
from app.core.normalization import payload_hash as record_digest
from app.core.provenance import current_context_id
from app.core.timeutil import as_utc
from app.db.contracts import SessionFactory, TransactionSessions
from app.db.dialect import dialect_insert
from app.garmin.client import GarminClient, GarminError, GarminExchange
from app.models.garmin import (
    GarminApiResponse,
    GarminPullState,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.services.readers import ArchiveReaders

from .garmin_activity_identity import activity_identifier
from .garmin_bounded import normalize_bounded
from .garmin_kinds import ACTIVITY_PAGE, BY_NAME, iso
from .ingestion_contracts import IngestionSource, Stream, StreamStatus
from .ingestion_readers import GARMIN_ARCHIVE

logger = logging.getLogger(__name__)

# Per date chunk, independent of an optional caller budget. At 100 activities per
# page this bounds a broken search to 100,000 returned records.
MAX_ACTIVITY_PAGES = 1000

# Two parameters per version plus kind: 801 binds, below the historical 999-variable
# limit and PostgreSQL/asyncpg's 32767-parameter limit, including legacy aliases.
_VERSION_BATCH = 400


class GarminPaginationError(GarminError):
    """An incomplete activity search; raised only after yielding its last response.

    Messages contain neither record contents, fingerprints nor request parameters,
    so the existing per-kind failure reporting can safely persist them.
    """


class _ActivityPages:
    """Track full-page content within one fetch, with bounded digest-only history.

    The request cap bounds the set as well as the number of HTTP requests. Retain
    all earlier full-page fingerprints up to that cap, so A→B→A is detected too.
    A caller's smaller budget can still pause this attempt independently.
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.count = 0

    def check(self, records: list[object]) -> GarminPaginationError | None:
        """Inspect a full page without rejecting overlapping or revised records.

        Sort the record digests, retaining duplicate counts: record and object-key
        order do not matter, but every record's complete content does. Never keep
        bodies in the history or include their digests in the resulting error.
        """
        self.count += 1
        fingerprint = _page_fingerprint(records)
        if fingerprint in self.seen:
            return GarminPaginationError("Garmin activity search returned a repeated full page")
        if self.count >= MAX_ACTIVITY_PAGES:
            return GarminPaginationError("Garmin activity search exceeded its page limit")
        self.seen.add(fingerprint)
        return None


def _page_fingerprint(records: list[object]) -> str:
    # Escaping also handles JSON strings with lone surrogates: even records that
    # cannot enter JSONB must participate without preventing response archival.
    digests = (
        hashlib.sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=True).encode("ascii")
        ).digest()
        for record in records
    )
    return hashlib.sha256(b"".join(sorted(digests))).hexdigest()


@dataclass(frozen=True)
class GarminPage:
    data_points: list[object]
    next_page_token: str | None
    exchanges: list[GarminExchange]
    fallback_key: str | None = None
    cyclic: bool = False


class GarminSource(IngestionSource[GarminPage]):
    @property
    def name(self) -> str:
        return "garmin"

    @property
    def readers(self) -> ArchiveReaders:
        return GARMIN_ARCHIVE

    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        *,
        client: GarminClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self._client = client
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def client(self) -> GarminClient:
        if self._client is None:
            self._client = GarminClient(self.settings.garmin_api_key_file)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        stream: Stream,
        window: FetchWindow,
        *,
        on_reset: Callable[[], Awaitable[None]] | None = None,
        budget: FetchBudget | None = None,
        on_request: Callable[[], None] | None = None,
    ) -> AsyncIterator[GarminPage]:
        kind = BY_NAME[stream.name]
        start, end = (
            as_utc(window.start).date(),
            (as_utc(window.end) - timedelta(microseconds=1)).date(),
        )
        path, params = kind.request(start, end)
        page = int(window.page_token or 0)
        activity_pages = _ActivityPages()
        while True:
            if budget is not None and budget.remaining <= 0:
                raise PaginationPaused("Ingestion page budget exhausted")
            if kind.mode == "search":
                params = {**params, "start": page * ACTIVITY_PAGE}
            if on_request is not None:
                on_request()
            exchanges: list[GarminExchange] = []
            try:
                response = await self.client.get(
                    path,
                    params,
                    kind=kind.name,
                    context_id=current_context_id(),
                    exchanges=exchanges,
                )
                records = kind.records(response.json)
            except GarminError:
                # HTTP and outer-shape errors are evidence without a record page.
                # Archive once before propagating; never ingest an invalid envelope.
                if exchanges:
                    async with self.sessions.begin() as session:
                        await _insert_exchanges(session, exchanges)
                raise
            if budget is not None:
                budget.remaining -= 1
            more = kind.mode == "search" and len(records) >= ACTIVITY_PAGE
            failure = activity_pages.check(records) if more else None
            yield GarminPage(
                records,
                str(page + 1) if more else None,
                response.exchanges,
                iso(start) if kind.mode == "day" else None,
            )
            # ingest_pages must commit the stopped response and all storable records
            # before the failure propagates. Its continuation stays nonterminal;
            # run/backfill must not mark this date chunk successful or advance it.
            if failure is not None:
                raise failure
            if not more:
                return
            page += 1

    async def keep_after(self, page: GarminPage, failure: BaseException) -> None:
        # A failed record transaction must not discard the response bytes it received.
        async with self.sessions.begin() as session:
            await _insert_exchanges(session, page.exchanges)

    async def status(self, session: AsyncSession) -> dict[str, StreamStatus]:
        return {
            state.kind: StreamStatus(state.last_success_at, state.last_error, state.last_error_at)
            for state in await session.scalars(select(GarminPullState))
        }

    async def normalize(
        self,
        window: FetchWindow,
        *,
        sessions: TransactionSessions | None = None,
        seen_since: datetime | None = None,
    ) -> NormalizeStats:
        """Normalize the archive's newest records of ``start``..``end`` (idempotent), in
        bounded transactions: re-running after an interruption writes only what is missing.

        A pull passes its ownership capability explicitly. It reaches every daily chunk
        and activity page; the pooled factory archives HTTP evidence independently.
        Standalone callers may omit the capability and use the source factory.
        This stage does not acquire ownership itself; the orchestrator supplies it."""
        return await normalize_bounded(
            sessions if sessions is not None else self.sessions,
            as_utc(window.start).date(),
            (as_utc(window.end) - timedelta(microseconds=1)).date(),
            device=self.settings.garmin_device_name,
            seen_since=seen_since,
        )

    async def store_raw(
        self,
        session: AsyncSession,
        stream: Stream,
        page: GarminPage,
        *,
        fetch_started: datetime | None = None,
    ) -> StoreStats:
        """Archive a response and its records in the caller's page transaction."""
        kind = BY_NAME[stream.name]
        now = self.clock()
        response_id = await _insert_exchanges(session, page.exchanges)
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        unstorable = 0
        for record in page.data_points:
            if not jsonb_storable(record):
                unstorable += 1
                continue
            digest = record_digest(record)
            key = kind.key(record) or page.fallback_key or digest[:32]
            rows.setdefault(
                (key, digest),
                {
                    "kind": kind.name,
                    "key": key,
                    "body": record,
                    "body_sha256": digest,
                    "response_id": response_id,
                    "first_seen_at": now,
                },
            )
        if unstorable:
            event(logger, "GARMIN_RECORDS_UNSTORABLE", kind=kind.name, count=unstorable)
        if not rows:
            return StoreStats(rejected=unstorable)
        if response_id is None:
            raise ValueError("Garmin record observations require a response")
        if kind.name == "activities":
            rows = await _reuse_activity_versions(session, rows)
        existing = await _version_ids(session, kind.name, rows)
        new_rows = [row for pair, row in rows.items() if pair not in existing]
        if new_rows:
            statement = dialect_insert(session, GarminRawRecord.__table__)
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=["kind", "key", "body_sha256"]),
                new_rows,
            )
        await _observe_records(session, kind.name, rows, response_id, now)
        return StoreStats(inserted=len(new_rows), rejected=unstorable)


async def _reuse_activity_versions(
    session: AsyncSession, rows: dict[tuple[str, str], dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Observe an identical legacy version instead of duplicating or rewriting it.

    A prefix alone is never a match: the digest must cover the entire body, including
    its full identifier. Prefer an existing canonical version when both are present.
    """
    aliases = {}
    for pair, row in rows.items():
        identifier = activity_identifier(row["body"])
        if identifier is not None and len(identifier) > 64:
            aliases[pair] = (identifier[:64], pair[1])
    if not aliases:
        return rows
    existing = await _version_ids(session, "activities", (*aliases, *aliases.values()))
    reused = {}
    for pair, row in rows.items():
        legacy = aliases.get(pair)
        chosen = legacy if pair not in existing and legacy in existing else pair
        reused[chosen] = {**row, "key": chosen[0]}
    return reused


async def _observe_records(
    session: AsyncSession,
    kind: str,
    rows: dict[tuple[str, str], dict[str, Any]],
    response_id: int,
    observed_at: datetime,
) -> None:
    """Append sightings even for deduplicated versions, in the caller's page transaction."""
    identifiers = await _version_ids(session, kind, rows)
    await session.execute(
        insert(GarminRecordObservation),
        [
            {
                "raw_record_id": identifiers[pair],
                "response_id": response_id,
                "observed_at": observed_at,
            }
            for pair in rows
        ],
    )


async def _version_ids(
    session: AsyncSession, kind: str, pairs: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Read only page-requested versions using the unique (kind, key, digest) index.

    Both the result and intermediate collections are bounded by the incoming page,
    regardless of how often its keys were corrected in the archive. Deduplicate
    aliases before batching so shared prefixes do not cause redundant lookups.
    """
    requested = sorted(set(pairs))
    identifiers: dict[tuple[str, str], int] = {}
    for offset in range(0, len(requested), _VERSION_BATCH):
        records = await session.execute(
            select(GarminRawRecord.id, GarminRawRecord.key, GarminRawRecord.body_sha256).where(
                GarminRawRecord.kind == kind,
                tuple_(GarminRawRecord.key, GarminRawRecord.body_sha256).in_(
                    requested[offset : offset + _VERSION_BATCH]
                ),
            )
        )
        identifiers.update({(key, digest): identifier for identifier, key, digest in records})
    return identifiers


async def _insert_exchanges(session: AsyncSession, exchanges: list[GarminExchange]) -> int | None:
    """Append the exchanges to ``garmin_api_responses``; the id of the last one."""
    if not exchanges:
        return None
    table = cast(Table, GarminApiResponse.__table__)
    result = await session.execute(
        insert(table).returning(table.c.id, sort_by_parameter_order=True),
        [exchange.row() for exchange in exchanges],
    )
    ids = list(result.scalars().all())
    return ids[-1] if ids else None
