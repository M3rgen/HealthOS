"""Google's page adapter and the public provider ingestion contract.

Google stores raw and normalized rows in the same page transaction. Garmin defers
normalization until the archived calendar window is complete. Both use ingest_pages;
only the page evidence and the provider's storage/parser implementation differ.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fetch_window import FetchBudget, FetchWindow
from app.core.ingestion_stats import NormalizeStats
from app.db.contracts import TransactionSessions
from app.google_health.client import Page
from app.google_health.data_types import DataTypeSpec, supported_data_types
from app.google_health.pagination import PaginationOptions, iter_pages
from app.models.state import SyncState
from app.services.archive.source_deletions import mark_source_deletions
from app.services.archive.storage import StoreStats, store_page
from app.services.readers import ArchiveReaders

from .garmin_ingestion import GarminSource as GarminSource
from .ingestion_contracts import IngestionSource as IngestionSource
from .ingestion_contracts import Stream, StreamStatus
from .ingestion_readers import GOOGLE_ARCHIVE

if TYPE_CHECKING:
    from app.google_health.client import GoogleHealthClient


class StorePage(Protocol):
    def __call__(
        self,
        session: AsyncSession,
        spec: DataTypeSpec,
        page: Page,
        *,
        source: str | None = None,
        fetch_started: datetime | None = None,
    ) -> Awaitable[StoreStats]: ...


@runtime_checkable
class PageSource(IngestionSource[Page], Protocol):
    """Google's additional checkpoint and deletion-marking capabilities."""

    async def warm_up(self) -> None: ...

    async def mark_missing(
        self,
        session: AsyncSession,
        spec: DataTypeSpec,
        start: datetime,
        end: datetime,
        fetched_since: datetime,
    ) -> int: ...


class GoogleSource(PageSource):
    @property
    def name(self) -> str:
        return "google"

    def __init__(
        self,
        client: "GoogleHealthClient",
        *,
        store_raw: StorePage | None = None,
    ) -> None:
        self.client = client
        self._store_raw = store_raw

    @staticmethod
    def _spec(stream: Stream) -> DataTypeSpec:
        if isinstance(stream, DataTypeSpec):
            return stream
        return supported_data_types(extra=[stream.name])[stream.name]

    async def store_raw(
        self,
        session: AsyncSession,
        stream: Stream,
        page: Page,
        *,
        fetch_started: datetime | None = None,
    ) -> StoreStats:
        return await (self._store_raw or store_page)(
            session, self._spec(stream), page, fetch_started=fetch_started
        )

    def fetch(
        self,
        stream: Stream,
        window: FetchWindow,
        *,
        on_reset: Callable[[], Awaitable[None]] | None = None,
        budget: FetchBudget | None = None,
        on_request: Callable[[], None] | None = None,
    ) -> AsyncIterator[Page]:
        return iter_pages(
            self.client,
            self._spec(stream),
            window.start,
            window.end,
            PaginationOptions(window.page_token, budget, on_request),
            on_reset=on_reset,
        )

    async def normalize(
        self,
        window: FetchWindow,
        *,
        sessions: TransactionSessions | None = None,
        seen_since: datetime | None = None,
    ) -> NormalizeStats:
        """No deferred work: store_page normalized in the raw page's atomic transaction."""
        return NormalizeStats()

    async def status(self, session: AsyncSession) -> dict[str, StreamStatus]:
        return {
            state.data_type: StreamStatus(state.last_successful_sync, state.last_error)
            for state in await session.scalars(select(SyncState))
        }

    async def warm_up(self) -> None:
        await self.client.warm_up()

    async def keep_after(self, page: Page, failure: BaseException) -> None:
        await self.client.keep_after(page.responses, failure)

    async def mark_missing(
        self,
        session: AsyncSession,
        spec: DataTypeSpec,
        start: datetime,
        end: datetime,
        fetched_since: datetime,
    ) -> int:
        return await mark_source_deletions(session, spec, start, end, fetched_since)

    @property
    def readers(self) -> ArchiveReaders:
        return GOOGLE_ARCHIVE
