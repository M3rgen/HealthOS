"""Callable ingestion stages; only the evidence carried by a page varies by provider."""

# pyright: strict

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fetch_window import FetchBudget, FetchWindow
from app.core.ingestion_stats import NormalizeStats, StoreStats
from app.db.contracts import TransactionSessions
from app.services.readers import ArchiveReaders


class Stream(Protocol):
    @property
    def name(self) -> str: ...


class IngestionPage(Protocol):
    @property
    def data_points(self) -> Sequence[object]: ...

    @property
    def next_page_token(self) -> str | None: ...

    @property
    def cyclic(self) -> bool: ...


@dataclass(frozen=True)
class StreamStatus:
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None = None


@runtime_checkable
class IngestionSource[Page: IngestionPage](Protocol):
    @property
    def name(self) -> str: ...

    @property
    def readers(self) -> ArchiveReaders: ...

    def fetch(
        self,
        stream: Stream,
        window: FetchWindow,
        *,
        on_reset: Callable[[], Awaitable[None]] | None = None,
        budget: FetchBudget | None = None,
        on_request: Callable[[], None] | None = None,
    ) -> AsyncIterator[Page]: ...

    async def store_raw(
        self,
        session: AsyncSession,
        stream: Stream,
        page: Page,
        *,
        fetch_started: datetime | None = None,
    ) -> StoreStats: ...

    async def keep_after(self, page: Page, failure: BaseException) -> None: ...

    async def normalize(
        self,
        window: FetchWindow,
        *,
        sessions: TransactionSessions | None = None,
        seen_since: datetime | None = None,
    ) -> NormalizeStats:
        """Finish deferred work using the caller's transaction capability when supplied.

        Deferred writers must carry it through every short transaction, rather than
        opening an independent session. Atomic page writers have no deferred work.
        """
        ...

    async def status(self, session: AsyncSession) -> dict[str, StreamStatus]: ...


@dataclass(frozen=True)
class FetchControls:
    """Optional callbacks and request budget for one ingestion fetch."""

    on_reset: Callable[[], Awaitable[None]] | None = None
    budget: FetchBudget | None = None
    on_request: Callable[[], None] | None = None


async def ingest_pages[Page: IngestionPage](
    source: IngestionSource[Page],
    stream: Stream,
    window: FetchWindow,
    commit: Callable[[Page], Awaitable[StoreStats]],
    *,
    controls: FetchControls | None = None,
) -> AsyncIterator[tuple[Page, StoreStats]]:
    """Fetch and commit pages for every provider, preserving the caller's transaction.

    The commit callback calls store_raw and writes its checkpoint/receipt in that same
    transaction. A page becomes progress only after commit. Failed and cancelled writes
    hand their response evidence back to the provider before propagating the failure.
    """
    controls = controls or FetchControls()
    async for page in source.fetch(
        stream,
        window,
        on_reset=controls.on_reset,
        budget=controls.budget,
        on_request=controls.on_request,
    ):
        try:
            stats = await commit(page)
        except BaseException as failure:
            await source.keep_after(page, failure)
            raise
        yield page, stats
