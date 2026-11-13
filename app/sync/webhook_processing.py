from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, null, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import error_diagnostics, event
from app.core.provenance import provenance
from app.core.timeutil import as_utc
from app.core.webhook_types import INTERRUPTED_ERROR as INTERRUPTED_ERROR
from app.core.webhook_types import NotificationStatus
from app.db.contracts import TransactionSessions
from app.db.dialect import dialect_insert
from app.models.state import (
    JobStatus,
    SyncState,
    WebhookBatchItem,
    WebhookEvent,
    WebhookNotification,
)
from app.services.synchronization.webhook_ingestion import has_valid_element, prepare_elements
from app.services.synchronization.webhook_metadata import UNPARSED_BODY as UNPARSED_BODY
from app.services.synchronization.webhook_metadata import (
    NotificationWindowError,
    index_metadata,
    jsonb_storable,
    notification_data,
    notification_windows,
    parse_webhook_body,
)
from app.services.synchronization.webhook_status import WebhookStatusService
from app.sync.errors import error_code, error_summary
from app.sync.incremental import IncrementalSync, stale_running_cutoff
from app.sync.local_lock import LocalLock
from app.sync.ownership import NOTIFICATION_NAMESPACE, OwnershipLost, run_ownership
from app.sync.webhook_reads import batch_hashes, read_pages, unfinished_hashes

logger = logging.getLogger(__name__)

# A captured signed body stays cryptographically valid forever. A live re-delivery of a
# batch first verified longer ago than this cannot restart its processing; retries of
# old batches go through process_pending (the replay CLI) instead.
SIGNED_REPLAY_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class IngestResult:
    batch_hash: str
    element_count: int
    inserted: bool
    should_process: bool


@dataclass
class ProcessingStats:
    batches: int = 0
    completed_elements: int = 0
    failed_elements: int = 0
    rejected_elements: int = 0


class WebhookBatchProcessor:
    """Durably ingest, deduplicate, process, and retry signed webhook batches."""

    def __init__(
        self,
        sessions: async_sessionmaker,
        incremental: IncrementalSync,
        supported_data_types: set[str],
        status: WebhookStatusService | None = None,
    ) -> None:
        self.sessions = sessions
        self.incremental = incremental
        self.supported_data_types = supported_data_types
        self.status = status
        # Only elements being processed right now have an entry; see _element_lock.
        self._element_locks: dict[str, LocalLock] = {}

    async def ingest_verified(self, raw: bytes, payload: object) -> IngestResult:
        """Persist an authenticated request without altering its signed raw bytes."""

        batch_hash = hashlib.sha256(raw).hexdigest()
        return await self._prepare_batch(
            batch_hash,
            payload,
            raw_body=raw,
            signature_verified_at=datetime.now(UTC),
        )

    async def prepare_existing_batch(self, batch_hash: str) -> IngestResult:
        """Materialize per-element state for a previously verified legacy batch."""

        async with self.sessions() as session:
            stored = await session.get(WebhookEvent, batch_hash)
            if stored is None:
                raise ValueError(f"Unknown webhook batch: {batch_hash}")
            payload = stored.raw_json
            if payload is None and stored.raw_body is not None:
                # A body-only archive may also exceed the parser's limits.
                payload = parse_webhook_body(stored.raw_body)
        return await self._prepare_batch(
            batch_hash,
            payload,
            raw_body=None,
            signature_verified_at=None,
        )

    async def _prepare_batch(
        self,
        batch_hash: str,
        payload: object,
        *,
        raw_body: bytes | None,
        signature_verified_at: datetime | None,
    ) -> IngestResult:
        elements = payload if isinstance(payload, list) else [payload]
        now = datetime.now(UTC)
        inserted = False
        prepared = False
        async with self.sessions.begin() as session:
            if raw_body is not None:
                inserted = await _insert_batch(
                    session, batch_hash, payload, elements, raw_body, signature_verified_at or now
                )
            batch = await session.get(WebhookEvent, batch_hash)
            if batch is None:
                raise RuntimeError("Webhook batch could not be persisted")
            if not inserted and raw_body is not None and _stale(batch.signature_verified_at, now):
                if self.status:
                    self.status.observe(session, "expired_replay")
                event(logger, "WEBHOOK_REPLAY_IGNORED", batch_hash=batch_hash)
                return IngestResult(
                    batch_hash=batch_hash,
                    element_count=len(elements),
                    inserted=False,
                    should_process=False,
                )
            if raw_body is not None and self.status and not inserted:
                self.status.observe(session, "duplicate")
            existing_items = await session.scalar(
                select(func.count())
                .select_from(WebhookBatchItem)
                .where(WebhookBatchItem.batch_hash == batch_hash)
            )
            if not existing_items:
                await prepare_elements(
                    session,
                    batch_hash,
                    elements,
                    self.supported_data_types,
                    self.incremental.specs,
                    self.status,
                )
                batch.element_count = len(elements)
                if raw_body is not None and batch.raw_body is None:
                    batch.raw_body = raw_body
                if signature_verified_at is not None and batch.signature_verified_at is None:
                    batch.signature_verified_at = signature_verified_at
                prepared = True

            incomplete = await session.scalar(
                select(func.count())
                .select_from(WebhookBatchItem)
                .join(
                    WebhookNotification,
                    WebhookNotification.element_hash == WebhookBatchItem.element_hash,
                )
                .where(
                    WebhookBatchItem.batch_hash == batch_hash,
                    WebhookNotification.is_valid.is_(True),
                    WebhookNotification.status != NotificationStatus.completed,
                )
            )
            if raw_body is not None and self.status:
                valid = await has_valid_element(session, batch_hash)
                if valid:
                    self.status.observe(session, "notification_received")
        return IngestResult(
            batch_hash=batch_hash,
            element_count=len(elements),
            inserted=inserted,
            should_process=prepared or bool(incomplete),
        )

    async def process_batch(self, batch_hash: str) -> ProcessingStats:
        try:
            return await self._process_batch(batch_hash)
        except Exception:
            if self.status:
                await self.status.record("batch_processing_failed")
            raise

    async def _process_batch(self, batch_hash: str) -> ProcessingStats:
        async with self.sessions() as session:
            hashes = (
                await session.scalars(
                    select(WebhookBatchItem.element_hash)
                    .where(WebhookBatchItem.batch_hash == batch_hash)
                    .order_by(WebhookBatchItem.ordinal)
                )
            ).all()
        stats = ProcessingStats(batches=1)
        for element_hash in dict.fromkeys(hashes):
            result = await self._process_element(element_hash)
            if result == NotificationStatus.completed:
                stats.completed_elements += 1
            elif result == NotificationStatus.failed:
                stats.failed_elements += 1
            elif result == NotificationStatus.rejected:
                stats.rejected_elements += 1
        await self._finalize_batch(batch_hash)
        return stats

    @asynccontextmanager
    async def _element_lock(self, element_hash: str) -> AsyncIterator[None]:
        """Serialize work on one element without keeping a lock per element forever.

        The entry is counted before awaiting the lock and dropped only when no coroutine
        holds or waits for it. There is no await between the lookup and the count, so a
        newcomer can never pick up an entry that is about to be removed.
        """
        entry = self._element_locks.get(element_hash)
        if entry is None:
            entry = self._element_locks[element_hash] = LocalLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._element_locks.get(element_hash) is entry:
                del self._element_locks[element_hash]

    async def _process_element(self, element_hash: str) -> NotificationStatus:
        async with self._element_lock(element_hash):
            # Keep ownership from before the claim through every window and result.
            # Recovery never waits for this lock or takes the data-type lock. A delivery
            # queues for a free owner slot: only another owner of this element is "running".
            async with run_ownership(
                self.sessions,
                element_hash,
                wait=False,
                namespace=NOTIFICATION_NAMESPACE,
                wait_for_slot=True,
            ) as sessions:
                if sessions is None:
                    return NotificationStatus.running
                return await self._process_owned_element(element_hash, sessions)

    async def _process_owned_element(
        self, element_hash: str, sessions: TransactionSessions
    ) -> NotificationStatus:
        async with sessions.begin() as session:
            notification = await session.get(WebhookNotification, element_hash)
            if notification is None:
                raise RuntimeError("Webhook notification is missing") from None
            if not notification.is_valid:
                return NotificationStatus.rejected
            if notification.status == NotificationStatus.completed:
                return NotificationStatus.completed
            claim = await session.execute(
                update(WebhookNotification)
                .where(
                    WebhookNotification.element_hash == element_hash,
                    WebhookNotification.status.in_(
                        (
                            NotificationStatus.pending,
                            NotificationStatus.failed,
                            NotificationStatus.interrupted,
                        )
                    ),
                )
                .values(
                    status=NotificationStatus.running,
                    attempt_count=WebhookNotification.attempt_count + 1,
                    last_error=None,
                )
                .returning(
                    WebhookNotification.data_type,
                    WebhookNotification.intervals,
                )
            )
            claimed = claim.one_or_none()
        if claimed is None:
            return NotificationStatus.running
        data_type, intervals = claimed
        try:
            # Archived pending/failed notifications predate ingestion validation.
            # Recheck under ownership and before counting or starting any fetch.
            windows = notification_windows(self.incremental.specs[data_type], intervals)
            await self._run_incremental(element_hash, data_type, windows, sessions)
            async with sessions.begin() as session:
                notification = await session.get(WebhookNotification, element_hash)
                if notification is None:
                    raise RuntimeError("Webhook notification is missing") from None
                notification.status = NotificationStatus.completed
                notification.processed_at = datetime.now(UTC)
                notification.last_error = None
            event(logger, "WEBHOOK_ELEMENT_PROCESSED", data_type=data_type)
            result = NotificationStatus.completed
        except NotificationWindowError as exc:
            async with sessions.begin() as session:
                notification = await session.get(WebhookNotification, element_hash)
                if notification is None:
                    raise RuntimeError("Webhook notification is missing") from None
                notification.is_valid = False
                notification.status = NotificationStatus.rejected
                notification.validation_error = str(exc)
                notification.last_error = None
                if self.status:
                    self.status.observe(session, "notification_rejected")
            result = NotificationStatus.rejected
        except Exception as exc:
            await self._record_element_failure(element_hash, data_type, exc, sessions)
            result = NotificationStatus.failed
        await self._finalize_related_batches(element_hash, sessions)
        return result

    async def _record_element_failure(
        self, element_hash: str, data_type: str, exc: Exception, sessions: TransactionSessions
    ) -> None:
        """Keep only safe diagnostics at the processing boundary.

        Database exceptions can embed raw records in their SQL parameters. Batch
        finalization copies this summary, so truncating exception text is not enough.
        Signed bodies and Google responses remain untouched in their raw archives.
        """
        summary = error_summary(exc)
        async with sessions.begin() as session:
            notification = await session.get(WebhookNotification, element_hash)
            if notification is None:
                raise RuntimeError("Webhook notification is missing") from None
            notification.status = NotificationStatus.failed
            notification.last_error = summary
            if self.status:
                self.status.observe(session, "processing_failed")
        # Never attach the exception, its traceback or its cause/context to the record;
        # formatting and downstream handlers must receive only these safe fields.
        logger.error(
            "Webhook notification processing failed",
            extra={
                "event": "WEBHOOK_ELEMENT_FAILED",
                "data_type": data_type,
                "error_type": type(exc).__name__,
                "error_code": error_code(exc),
                **error_diagnostics(exc),
            },
        )

    async def _run_incremental(
        self,
        element_hash: str,
        data_type: str,
        windows: list[tuple[datetime | None, datetime | None]],
        sessions: TransactionSessions,
    ) -> None:
        async def check_owner() -> None:
            # A lost backend must never reconnect through the application's pool.
            # IncrementalSync also checks this after waiting for data-type ownership.
            try:
                async with sessions.begin():
                    pass
            except SQLAlchemyError as exc:
                raise OwnershipLost("webhook ownership connection was lost") from exc

        for window_start, window_end in windows:
            async with sessions.begin() as session:
                await session.execute(
                    update(WebhookNotification)
                    .where(WebhookNotification.element_hash == element_hash)
                    .values(sync_attempt_count=WebhookNotification.sync_attempt_count + 1)
                )
            with provenance(source="webhook", context_id=f"webhook:{element_hash}"):
                await self.incremental.run(
                    data_type, window_start, window_end, check_owner=check_owner
                )
            async with sessions.begin() as session:
                await session.execute(
                    update(WebhookNotification)
                    .where(WebhookNotification.element_hash == element_hash)
                    .values(sync_success_count=WebhookNotification.sync_success_count + 1)
                )

    async def _finalize_related_batches(
        self, element_hash: str, sessions: TransactionSessions
    ) -> None:
        async with sessions.begin() as session:
            batch_hashes = (
                await session.scalars(
                    select(WebhookBatchItem.batch_hash).where(
                        WebhookBatchItem.element_hash == element_hash
                    )
                )
            ).all()
        for batch_hash in set(batch_hashes):
            await self._finalize_batch(batch_hash, sessions)

    async def _finalize_batch(
        self, batch_hash: str, sessions: TransactionSessions | None = None
    ) -> None:
        sessions = sessions if sessions is not None else self.sessions
        async with sessions.begin() as session:
            # Serialize summaries before reading notification outcomes. A finalizer
            # queued behind a newer retry must derive its result from that retry.
            batch = await session.get(WebhookEvent, batch_hash, with_for_update=True)
            if batch is None:
                return
            rows = (
                await session.execute(
                    select(
                        WebhookNotification.is_valid,
                        WebhookNotification.status,
                        WebhookNotification.validation_error,
                        WebhookNotification.last_error,
                    )
                    .select_from(WebhookBatchItem)
                    .join(
                        WebhookNotification,
                        WebhookNotification.element_hash == WebhookBatchItem.element_hash,
                    )
                    .where(WebhookBatchItem.batch_hash == batch_hash)
                )
            ).all()
            incomplete = [
                row for row in rows if row.is_valid and row.status != NotificationStatus.completed
            ]
            rejected = [row for row in rows if not row.is_valid]
            failed = [
                row
                for row in rows
                if row.is_valid
                and row.status in (NotificationStatus.failed, NotificationStatus.interrupted)
            ]
            batch.failure_count = len(rejected) + len(failed)
            errors = [row.validation_error for row in rejected] + [row.last_error for row in failed]
            batch.last_error = "; ".join(error for error in errors if error)[:2000] or None
            batch.processed_at = None if incomplete else datetime.now(UTC)

    async def recover_running(self, now: datetime | None = None) -> int:
        """Fail abandoned elements under nonblocking notification ownership.

        Timestamps only select candidates. A live owner remains protected while waiting
        for Google, for a data-type lock, and between windows, however old its heartbeat.
        Recheck candidates under ownership so a concurrent retry cannot be failed.
        """
        cutoff = stale_running_cutoff(now)
        live_types = select(SyncState.data_type).where(
            SyncState.status == JobStatus.running, SyncState.updated_at >= cutoff
        )
        conditions = (
            WebhookNotification.status == NotificationStatus.running,
            WebhookNotification.updated_at < cutoff,
            or_(
                WebhookNotification.data_type.is_(None),
                WebhookNotification.data_type.not_in(live_types),
            ),
        )
        recovered = 0
        query = select(WebhookNotification.element_hash).where(*conditions)
        async for page in read_pages(self.sessions, query, WebhookNotification.element_hash):
            for row in page:
                recovered += await self._recover_element(row.element_hash, conditions)
        if recovered:
            event(logger, "WEBHOOK_ELEMENTS_RECOVERED", count=recovered)
        return recovered

    async def _recover_element(self, element_hash, conditions) -> int:
        async with run_ownership(
            self.sessions, element_hash, wait=False, namespace=NOTIFICATION_NAMESPACE
        ) as sessions:
            if sessions is None:
                return 0
            async with sessions.begin() as session:
                result = await session.execute(
                    update(WebhookNotification)
                    .where(WebhookNotification.element_hash == element_hash, *conditions)
                    .values(status=NotificationStatus.interrupted, last_error=INTERRUPTED_ERROR)
                )
                count = cast(CursorResult[Any], result).rowcount or 0
                if count and self.status:
                    self.status.observe(session, "processing_interrupted")
            return count

    async def process_unfinished(
        self, *, min_age: timedelta = timedelta(minutes=5)
    ) -> ProcessingStats:
        """Finish elements a delivery left behind, without waiting for the operator's replay.

        Only ``pending`` elements and those recovery marked interrupted: a real failure stays
        for ``process_pending``. Cancelled work (a shutdown) and a lost spawned task leave
        exactly these. ``min_age`` leaves a delivery's own task to finish first; the shared
        element lock and ownership would keep them apart anyway.
        """
        cutoff = datetime.now(UTC) - min_age
        stats = ProcessingStats()
        attempted = 0
        async for element_hash in unfinished_hashes(self.sessions, cutoff):
            attempted += 1
            result = await self._process_element(element_hash)
            stats.completed_elements += result == NotificationStatus.completed
            stats.failed_elements += result == NotificationStatus.failed
            stats.rejected_elements += result == NotificationStatus.rejected
        if attempted:
            event(logger, "WEBHOOK_UNFINISHED_RETRIED", count=attempted)
        return stats

    async def process_pending(self, *, recover_running: bool = True) -> ProcessingStats:
        """Hydrate legacy batches, retry failed work, and preserve completed elements."""

        async for batch_hash in batch_hashes(self.sessions, unprepared=True):
            await self.prepare_existing_batch(batch_hash)
        if recover_running:
            await self.recover_running()
        total = ProcessingStats()
        async for batch_hash in batch_hashes(self.sessions):
            result = await self.process_batch(batch_hash)
            total.batches += result.batches
            total.completed_elements += result.completed_elements
            total.failed_elements += result.failed_elements
            total.rejected_elements += result.rejected_elements
        return total


def _stale(verified_at: datetime | None, now: datetime) -> bool:
    if verified_at is None:
        return True
    return now - as_utc(verified_at) > SIGNED_REPLAY_MAX_AGE


async def _insert_batch(
    session: AsyncSession,
    batch_hash: str,
    payload: object,
    elements: list,
    raw_body: bytes,
    signature_verified_at: datetime,
) -> bool:
    # Only a delivery inserts; prepare_existing_batch has its row already.
    # A body jsonb cannot hold is kept as its bytes alone (raw_json NULL).
    first_data = notification_data(elements[0]) if len(elements) == 1 else None
    statement = (
        dialect_insert(session, WebhookEvent)
        .values(
            payload_hash=batch_hash,
            **index_metadata(first_data),
            raw_json=payload if jsonb_storable(payload) else null(),
            raw_body=raw_body,
            signature_verified_at=signature_verified_at,
            element_count=len(elements),
            failure_count=0,
        )
        .on_conflict_do_nothing(index_elements=["payload_hash"])
        .returning(WebhookEvent.payload_hash)
    )
    return (await session.scalar(statement)) is not None
