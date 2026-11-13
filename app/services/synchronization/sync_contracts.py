"""Capabilities consumed by services; sync implementations are wired by app.runtime."""

from collections.abc import Coroutine
from typing import Any, Protocol

from app.core.ingestion_stats import StoreStats


class SyncRunner(Protocol):
    @property
    def sequence(self) -> int: ...

    def run(self, data_type: str) -> Coroutine[Any, Any, StoreStats]: ...

    def run_all(self) -> Coroutine[Any, Any, dict]: ...


class ReconciliationRunner(Protocol):
    async def run(self, days: int) -> dict: ...


class UnfinishedWebhooks(Protocol):
    async def process_unfinished(self) -> object: ...


class ScheduledWatchdog(Protocol):
    async def periodic_check(self) -> object: ...


class SyncProgress(Protocol):
    @property
    def last_runs(self) -> dict[str, dict]: ...


class RuntimeProgress(Protocol):
    @property
    def incremental(self) -> SyncProgress: ...
