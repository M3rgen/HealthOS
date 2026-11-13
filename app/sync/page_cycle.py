"""The fetch/store/receipt cycle shared by incremental sync and backfill."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fetch_window import FetchBudget, FetchWindow
from app.core.ingestion_stats import StoreStats
from app.google_health.client import Page
from app.google_health.data_types import DataTypeSpec
from app.services.providers.ingestion import PageSource
from app.services.providers.ingestion_contracts import FetchControls, ingest_pages
from app.services.synchronization.retrieval import ReceiptWriter
from app.sync.errors import retry_database_conflicts
from app.sync.rounds import FetchTiming


@dataclass
class PageCycle:
    source: PageSource
    spec: DataTypeSpec
    receipt: ReceiptWriter
    timing: FetchTiming
    budget: FetchBudget | None = None

    async def run(
        self,
        window: FetchWindow,
        checkpoint: Callable[[AsyncSession, Page], Awaitable[None]],
        *,
        on_commit: Callable[[StoreStats], None] | None = None,
        report: Callable[[AsyncSession, StoreStats], Awaitable[None]] | None = None,
    ) -> int:
        """Return current-attempt rejections when collecting committed progress.

        Raw rows, the lane's checkpoint, receipt and optional report commit together,
        in that order. Retry the whole transaction before counting progress. On
        cancellation, remember the received page before ingest_pages preserves its
        response; the lane then writes the failure outcome.
        """
        rejected = 0

        async def restart() -> None:
            nonlocal rejected
            rejected = 0
            self.timing.round_started = await self.receipt.reset()
            # A reset fetches the entire window, including a saved-token continuation.
            self.timing.fetched_since = self.timing.round_started

        async def commit(page: Page) -> StoreStats:
            async def store() -> StoreStats:
                async with self.receipt.sessions.begin() as session:
                    stats = await self.source.store_raw(
                        session, self.spec, page, fetch_started=self.timing.round_started
                    )
                    await checkpoint(session, page)
                    await self.receipt.page(session, page)
                    if report is not None:
                        await report(session, stats)
                return stats

            try:
                return await retry_database_conflicts(store)
            except BaseException:
                self.receipt.received = page
                raise

        async for page, stats in ingest_pages(
            self.source,
            self.spec,
            window,
            commit,
            controls=FetchControls(on_reset=restart, budget=self.budget),
        ):
            if on_commit is not None:
                on_commit(stats)
                rejected += stats.rejected
            self.receipt.committed(page)
        return rejected
