"""Coalesce manual sync and backfill starts before creating coroutines or acquiring connections."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from .sync_contracts import SyncRunner


class _HasTask(Protocol):
    @property
    def task(self) -> asyncio.Task: ...


class BackfillRunner(Protocol):
    def run(self, data_types: list[str] | None = None) -> Coroutine[Any, Any, int]: ...


@dataclass(frozen=True)
class SyncJob:
    job_id: str
    sequence: int
    task: asyncio.Task


@dataclass(frozen=True)
class BackfillJob:
    task: asyncio.Task


def _coalesced[J: _HasTask](
    active: dict[str | None, J], key: str | None, build: Callable[[], J]
) -> J:
    # No await between lookup and registration: simultaneous requests in the
    # application's event loop see the same task, including while it is queued.
    existing = active.get(key)
    if existing is not None and not existing.task.done():
        return existing
    job = build()
    active[key] = job

    def finished(task: asyncio.Task) -> None:
        # A new job may already have replaced this completed task.
        if active.get(key) is job:
            del active[key]

    job.task.add_done_callback(finished)
    return job


class SyncJobs:
    def __init__(self) -> None:
        self.active: dict[str | None, SyncJob] = {}

    def start(
        self,
        data_type: str | None,
        incremental: SyncRunner,
        spawn: Callable[[Coroutine[Any, Any, Any]], asyncio.Task],
    ) -> SyncJob:
        def build() -> SyncJob:
            sequence = incremental.sequence
            coroutine = incremental.run_all() if data_type is None else incremental.run(data_type)
            return SyncJob(uuid4().hex, sequence, spawn(coroutine))

        return _coalesced(self.active, data_type, build)


class BackfillJobs:
    """One running backfill per request key (a data type, or None for every type).

    A later request while it runs joins it: the running job is left alone, and only a request
    made after it has finished starts another. Each run spends its own page budget, so an
    uncoalesced burst of requests multiplied what one backfill can spend."""

    def __init__(self) -> None:
        self.active: dict[str | None, BackfillJob] = {}

    def start(
        self,
        data_type: str | None,
        runner: BackfillRunner,
        spawn: Callable[[Coroutine[Any, Any, Any]], asyncio.Task],
    ) -> BackfillJob:
        def build() -> BackfillJob:
            return BackfillJob(spawn(runner.run([data_type] if data_type else None)))

        return _coalesced(self.active, data_type, build)
