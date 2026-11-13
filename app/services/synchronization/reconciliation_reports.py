"""Reconciliation lifecycle and transaction-bound difference counters.

An unfinished run stays running, never successful. Counters share the raw page or
deletion transaction, including its rollback. A scope follows the caller's requested
window only; incremental disables it while finishing an older foreign checkpoint.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fetch_window import PaginationPaused
from app.core.ingestion_stats import StoreStats
from app.db.contracts import SessionFactory
from app.models.reconciliation import ReconciliationResult as Result
from app.models.reconciliation import ReconciliationRun as Run
from app.schemas.status import ReconciliationHistory, ReconciliationRun, ReconciliationType

_scope: ContextVar[tuple[uuid.UUID, str] | None] = ContextVar("reconciliation_report", default=None)


@contextmanager
def reporting(run_id: uuid.UUID | None, data_type: str = "") -> Iterator[None]:
    token = _scope.set((run_id, data_type) if run_id else None)
    try:
        yield
    finally:
        _scope.reset(token)


async def record_changes(
    session: AsyncSession, data_type: str, stats: StoreStats, *, page: bool = False
) -> None:
    """Must be called inside the transaction that made these changes."""
    scope = _scope.get()
    if scope is None or scope[1] != data_type:
        return
    result = await session.execute(
        update(Result)
        .where(Result.run_id == scope[0], Result.data_type == data_type, Result.status == "running")
        .values(
            inserted=Result.inserted + stats.inserted,
            updated=Result.updated + stats.updated,
            unchanged=Result.unchanged + stats.unchanged,
            source_deleted=Result.source_deleted + stats.source_deleted,
            pages_committed=Result.pages_committed + int(page),
        )
    )
    if cast(CursorResult, result).rowcount != 1:
        raise RuntimeError("Reconciliation result is not running")


async def begin_run(
    sessions: SessionFactory,
    run_id: uuid.UUID,
    start: datetime,
    end: datetime,
    started: datetime,
    data_types: Iterable[str],
) -> None:
    async with sessions.begin() as session:
        session.add(Run(id=run_id, requested_from=start, requested_to=end, started_at=started))
        await session.flush()
        session.add_all(Result(run_id=run_id, data_type=name) for name in data_types)


async def start_type(sessions: SessionFactory, run_id: uuid.UUID, data_type: str) -> None:
    async with sessions.begin() as session:
        row = await session.get(Result, (run_id, data_type))
        if row is None:
            raise RuntimeError("Reconciliation result is missing")
        row.status, row.started_at = "running", datetime.now(UTC)


async def finish_type(
    sessions: SessionFactory, run_id: uuid.UUID, data_type: str, failure: Exception | None = None
) -> None:
    async with sessions.begin() as session:
        row = await session.get(Result, (run_id, data_type))
        if row is None:
            raise RuntimeError("Reconciliation result is missing")
        row.status = (
            "completed"
            if failure is None
            else (
                "partial"
                if row.pages_committed or isinstance(failure, PaginationPaused)
                else "failed"
            )
        )
        row.error_type = type(failure).__name__[:100] if failure is not None else None
        row.finished_at = datetime.now(UTC)


async def finish_run(
    sessions: SessionFactory, run_id: uuid.UUID, failure: BaseException | None = None
) -> str:
    async with sessions.begin() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise RuntimeError("Reconciliation run is missing")
        rows = list((await session.scalars(select(Result).where(Result.run_id == run_id))).all())
        now = datetime.now(UTC)
        cancelled = isinstance(failure, asyncio.CancelledError)
        for row in rows:
            if row.status in {"pending", "running"}:
                row.status = "cancelled" if cancelled else "failed"
                row.error_type = type(failure).__name__[:100] if failure else "IncompleteResult"
                row.finished_at = now
        failures = [row for row in rows if row.status != "completed"]
        if cancelled:
            run.status = "cancelled"
        elif failures or failure:
            progress = any(row.pages_committed or row.status == "completed" for row in rows)
            run.status = "partial" if progress else "failed"
        else:
            run.status = "completed"
        run.error_type = (
            type(failure).__name__[:100]
            if failure
            else next((row.error_type for row in sorted(failures, key=lambda r: r.data_type)), None)
        )
        run.finished_at = now
        return run.status


async def recovery_errors(sessions: SessionFactory, run_id: uuid.UUID) -> tuple[str, ...]:
    """Read the exact recovery invocation, never a latest-run pointer or an in-memory result."""
    async with sessions() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return ("MissingReconciliationReport",)
        rows = list((await session.scalars(select(Result).where(Result.run_id == run_id))).all())
        if (
            run.status == "completed"
            and run.finished_at is not None
            and run.error_type is None
            and rows
            and all(
                row.status == "completed" and row.finished_at is not None and row.error_type is None
                for row in rows
            )
        ):
            return ()
        errors = tuple(sorted({row.error_type for row in rows if row.error_type}))
        return errors or (run.error_type or "IncompleteReconciliationReport",)


async def list_runs(
    session: AsyncSession, offset: int = 0, limit: int = 20
) -> ReconciliationHistory:
    if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
        raise ValueError("Invalid reconciliation pagination")
    runs = list(
        (
            await session.scalars(
                select(Run)
                .order_by(Run.started_at.desc(), Run.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    results: dict[uuid.UUID, list[ReconciliationType]] = {run.id: [] for run in runs}
    if runs:
        rows = await session.scalars(
            select(Result)
            .where(Result.run_id.in_(results))
            .order_by(Result.run_id, Result.data_type)
        )
        for row in rows:
            results[row.run_id].append(
                ReconciliationType(
                    data_type=row.data_type,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                    status=row.status,
                    inserted=row.inserted,
                    updated=row.updated,
                    unchanged=row.unchanged,
                    source_deleted=row.source_deleted,
                    pages_committed=row.pages_committed,
                    error_type=row.error_type,
                )
            )
    return {
        "runs": [
            ReconciliationRun(
                id=run.id,
                requested_from=run.requested_from,
                requested_to=run.requested_to,
                started_at=run.started_at,
                finished_at=run.finished_at,
                status=run.status,
                error_type=run.error_type,
                types=results[run.id],
            )
            for run in runs
        ],
        "offset": offset,
        "limit": limit,
        "total": (await session.scalar(select(func.count()).select_from(Run))) or 0,
    }
