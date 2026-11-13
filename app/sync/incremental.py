from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.ingestion_stats import StoreStats
from app.core.logging import event
from app.core.provenance import current_context_id, new_context_id, provenance
from app.core.timeutil import as_utc
from app.db.contracts import TransactionSessions
from app.google_health.client import Page
from app.google_health.data_types import DataTypeSpec
from app.google_health.fetch_window import FetchWindow
from app.google_health.pagination import PageBudget, PaginationPaused
from app.models.state import JobStatus, SyncState, fetch_round_start
from app.services.providers.ingestion import PageSource
from app.services.synchronization.reconciliation_reports import record_changes, reporting
from app.services.synchronization.retrieval import ReceiptWriter
from app.sync.checkpoints import incremental_start
from app.sync.errors import error_code, error_summary, is_transient
from app.sync.ownership import CheckedSessions, OwnershipLost, run_ownership
from app.sync.page_cycle import PageCycle
from app.sync.rounds import FetchTiming, SyncRun

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "process restarted"
# Heartbeat age selects recovery candidates; ownership decides whether they are abandoned.
STALE_RUNNING_AFTER = timedelta(minutes=15)


def stale_running_cutoff(now: datetime | None = None) -> datetime:
    """`running` rows last updated before this instant are candidates for recovery."""
    return (now or datetime.now(UTC)) - STALE_RUNNING_AFTER


class IncrementalSync:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        source: PageSource,
        specs: dict[str, DataTypeSpec],
    ):
        self.settings = settings
        self.sessions = sessions
        self.source = source
        self.specs = specs
        # In-memory per-run outcomes so clients can tell when a run they started has finished.
        self.sequence = 0
        self.last_runs: dict[str, dict] = {}

    async def recover_interrupted(self, now: datetime | None = None) -> int:
        """Fail stale states only when no run still owns the data type.

        Recheck the heartbeat under nonblocking run ownership. Keep the durable page
        token so a later invocation can finish the interrupted window first.
        """
        cutoff = stale_running_cutoff(now)
        async with self.sessions() as session:
            candidates = list(
                await session.scalars(
                    select(SyncState.data_type).where(
                        SyncState.status == JobStatus.running, SyncState.updated_at < cutoff
                    )
                )
            )
        recovered = 0
        for data_type in candidates:
            async with run_ownership(self.sessions, data_type, wait=False) as sessions:
                if sessions is None:
                    continue
                async with sessions.begin() as session:
                    result = await session.execute(
                        update(SyncState)
                        .where(
                            SyncState.data_type == data_type,
                            SyncState.status == JobStatus.running,
                            SyncState.updated_at < cutoff,
                        )
                        .values(status=JobStatus.failed, last_error=INTERRUPTED_ERROR)
                    )
                recovered += cast(CursorResult[Any], result).rowcount or 0
        if recovered:
            event(logger, "SYNC_STATE_RECOVERED", count=recovered)
        return recovered

    def _finish(self, data_type: str, stats: StoreStats, failed: bool) -> None:
        self.sequence += 1
        self.last_runs[data_type] = {
            "sequence": self.sequence,
            "inserted": stats.inserted,
            "updated": stats.updated,
            "rejected": stats.rejected,
            "failed": failed,
        }

    async def run(
        self,
        data_type: str,
        requested_start: datetime | None = None,
        requested_end: datetime | None = None,
        *,
        check_owner: Callable[[], Awaitable[None]] | None = None,
    ) -> StoreStats:
        """Fetch a window; an interrupted earlier window is finished first.

        A saved page token belongs to the previously requested window. That window is
        completed as a first round, and the window requested by this call always runs
        afterwards as its own round, so a caller (e.g. a webhook element) only sees
        success once its own range was fetched. Responses are tagged with the caller's
        run id (webhook element, reconciliation) or a new ``sync:<uuid>``.
        """
        with provenance(context_id=current_context_id() or new_context_id("sync")):
            return await self._run(data_type, requested_start, requested_end, check_owner)

    async def _run(
        self,
        data_type: str,
        requested_start: datetime | None,
        requested_end: datetime | None,
        check_owner: Callable[[], Awaitable[None]] | None = None,
    ) -> StoreStats:
        if data_type not in self.specs:
            raise ValueError(f"Unsupported data type: {data_type}")
        async with run_ownership(self.sessions, data_type) as sessions:
            if sessions is None:
                raise OwnershipLost("sync run ownership was not acquired")
            if check_owner is not None:
                sessions = CheckedSessions(sessions, check_owner)
            spec = self.specs[data_type]
            request_end = requested_end or datetime.now(UTC)
            rounds, requested = await self._prepare_rounds(
                sessions, data_type, requested_start, request_end
            )
            total = StoreStats()
            budget = PageBudget()
            try:
                for round_start, round_end, page_token in rounds:
                    # Finish a foreign checkpoint without attributing its differences to
                    # this reconciliation's requested range. Keep the public stats contract.
                    scope = (
                        nullcontext() if (round_start, round_end) == requested else reporting(None)
                    )
                    with scope:
                        await self._fetch_round(
                            spec,
                            window=FetchWindow(round_start, round_end, page_token),
                            run=SyncRun(data_type, sessions, total, budget),
                        )
                await self._set_outcome(sessions, data_type, JobStatus.completed)
                self._finish(data_type, total, failed=False)
                event(
                    logger,
                    "SYNC_COMPLETED",
                    data_type=data_type,
                    records=total.inserted + total.updated,
                )
                return total
            except PaginationPaused:
                await self._set_outcome(sessions, data_type, JobStatus.pending)
                # Preserve the existing outcome contract: an incomplete invocation is
                # never reported as success, even though its pages committed normally.
                self._finish(data_type, total, failed=True)
                event(logger, "SYNC_PAUSED", data_type=data_type)
                raise
            except Exception as exc:
                try:
                    await self._set_outcome(sessions, data_type, JobStatus.failed, exc)
                except OwnershipLost:
                    # A replacement owner may already be resuming; never fail its state.
                    pass
                self._finish(data_type, total, failed=True)
                event(
                    logger,
                    "SYNC_FAILED",
                    data_type=data_type,
                    error_type=type(exc).__name__,
                    error_code=error_code(exc),
                    transient=is_transient(exc),
                )
                raise

    async def _prepare_rounds(
        self,
        sessions: TransactionSessions,
        data_type: str,
        requested_start: datetime | None,
        request_end: datetime,
    ) -> tuple[list[tuple[datetime, datetime, str | None]], tuple[datetime, datetime]]:
        """Resume the durable checkpoint before this caller's window, sharing one budget."""
        async with sessions.begin() as session:
            state = await session.scalar(select(SyncState).where(SyncState.data_type == data_type))
            if state is None:
                state = SyncState(data_type=data_type)
                session.add(state)
            rounds: list[tuple[datetime, datetime, str | None]] = []
            # Finishing a foreign checkpoint can leave no budget for this window's
            # first page. Its pending range must still precede the next caller's.
            if (
                (state.last_page_token or state.status == JobStatus.pending)
                and state.last_requested_from
                and state.last_requested_to
            ):
                rounds.append(
                    (
                        as_utc(state.last_requested_from),
                        as_utc(state.last_requested_to),
                        state.last_page_token,
                    )
                )
            request_start = requested_start or incremental_start(
                state.last_successful_sync,
                self.settings.safety_window(data_type),
                request_end,
            )
            requested = (as_utc(request_start), as_utc(request_end))
            if not rounds or rounds[0][:2] != requested:
                rounds.append((*requested, None))
            state.status = JobStatus.running
            state.last_error = None
        event(
            logger,
            "SYNC_STARTED",
            data_type=data_type,
            requested_from=str(request_start),
            resumed=rounds[0][2] is not None,
        )
        return rounds, requested

    @staticmethod
    async def _set_outcome(
        sessions: TransactionSessions,
        data_type: str,
        status: JobStatus,
        failure: Exception | None = None,
    ) -> None:
        async with sessions.begin() as session:
            state = await session.get(SyncState, data_type)
            if state is None:
                raise RuntimeError("Incremental sync state is missing") from None
            state.status = status
            state.last_error = error_summary(failure) if failure is not None else None

    async def _fetch_round(self, spec: DataTypeSpec, *, window: FetchWindow, run: SyncRun) -> None:
        async with run.sessions.begin() as session:
            state = await session.get(SyncState, run.data_type)
            if state is None:
                raise RuntimeError("Incremental sync state is missing")
            state.last_requested_from = window.start
            state.last_requested_to = window.end
            state.last_page_token = window.page_token
            # A spent budget only records this range as pending: no fetch start or
            # receipt is begun for a round that cannot request its first page.
            paused = run.budget.remaining <= 0
            round_started: datetime | None = None
            if not paused:
                # A resumed round keeps the time its first page was fetched: "seen in
                # this fetch" always means since the round's real start.
                round_started = as_utc(
                    await fetch_round_start(
                        session,
                        "sync",
                        run.data_type,
                        fresh=window.page_token is None,
                        now=datetime.now(UTC),
                    )
                )
        if round_started is None:
            raise PaginationPaused("Google Health page budget exhausted")
        # Only a fetch that starts at the first page sees the whole window, so only
        # then can records that stopped appearing be marked as deleted at the source.
        fetched_since = round_started if window.page_token is None else None
        receipt = ReceiptWriter(
            run.sessions,
            spec,
            "sync",
            run.data_type,
            window=window,
        )
        await receipt.begin()
        try:
            await self._store_round(
                spec,
                FetchTiming(round_started, fetched_since),
                receipt,
                window=window,
                run=run,
            )
        except BaseException as failure:
            await receipt.failure(failure)
            raise

    async def _store_round(
        self,
        spec: DataTypeSpec,
        timing: FetchTiming,
        receipt: ReceiptWriter,
        *,
        window: FetchWindow,
        run: SyncRun,
    ) -> None:
        async def checkpoint(session: AsyncSession, page: Page) -> None:
            state = await session.get(SyncState, run.data_type)
            if state is None:
                raise RuntimeError("Incremental sync state is missing")
            if not page.cyclic:
                state.last_page_token = page.next_page_token
            state.records_downloaded += len(page.data_points)
            state.updated_at = datetime.now(UTC)

        async def report(session: AsyncSession, stats: StoreStats) -> None:
            await record_changes(session, run.data_type, stats, page=True)

        rejected = await PageCycle(self.source, spec, receipt, timing, run.budget).run(
            window, checkpoint, on_commit=run.total.add, report=report
        )
        await self.source.normalize(window)
        async with run.sessions.begin() as session:
            state = await session.get(SyncState, run.data_type)
            if state is None:
                raise RuntimeError("Incremental sync state is missing")
            previous_success = state.last_successful_sync
            # A widened webhook window may end in the future; the watermark never does.
            reached = min(window.end, datetime.now(UTC))
            state.last_successful_sync = (
                max(as_utc(previous_success), reached) if previous_success else reached
            )
            state.last_page_token = None
            deleted = 0
            # A rejected record may be an archived one, sent again: it was returned, but
            # this round cannot show it. The round still advances; marking waits for a
            # round without rejections (a record missing meanwhile is marked then).
            if timing.fetched_since is not None and not rejected:
                deleted = await self.source.mark_missing(
                    session, spec, window.start, window.end, timing.fetched_since
                )
            await record_changes(session, run.data_type, StoreStats(source_deleted=deleted))
            receipt.event(session, "completed")
        run.total.source_deleted += deleted

    async def run_all(self) -> dict[str, StoreStats | Exception]:
        semaphore = asyncio.Semaphore(self.settings.sync_concurrency)

        async def guarded(name: str):
            async with semaphore:
                try:
                    return await self.run(name)
                except Exception as exc:
                    return exc

        results = await asyncio.gather(*(guarded(name) for name in self.specs))
        return dict(zip(self.specs, results, strict=True))
