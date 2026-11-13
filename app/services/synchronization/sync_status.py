"""Sync checkpoints and grouped backfill progress, independent of HTTP routing."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.state import BackfillJob, JobStatus, SyncState
from app.schemas.status import BackfillStatus, SyncStatus

from .sync_contracts import RuntimeProgress


def _serialize(item: SyncState, runtime: RuntimeProgress) -> SyncStatus:
    return {
        "data_type": item.data_type,
        "status": item.status,
        "last_requested_from": item.last_requested_from,
        "last_requested_to": item.last_requested_to,
        "last_successful_sync": item.last_successful_sync,
        "records_downloaded": item.records_downloaded,
        "last_error": item.last_error,
        "last_run": runtime.incremental.last_runs.get(item.data_type),
    }


async def sync_statuses(session: AsyncSession, runtime: RuntimeProgress) -> list[SyncStatus]:
    rows = (await session.scalars(select(SyncState).order_by(SyncState.data_type))).all()
    return [_serialize(row, runtime) for row in rows]


async def sync_status(
    session: AsyncSession, data_type: str, runtime: RuntimeProgress
) -> SyncStatus | None:
    row = await session.get(SyncState, data_type)
    return _serialize(row, runtime) if row is not None else None


async def backfill_status(session: AsyncSession) -> list[BackfillStatus]:
    rows = (
        await session.execute(
            select(
                BackfillJob.data_type,
                BackfillJob.status,
                func.count(BackfillJob.id),
                func.sum(BackfillJob.records_downloaded),
            )
            .group_by(BackfillJob.data_type, BackfillJob.status)
            .order_by(BackfillJob.data_type, BackfillJob.status)
        )
    ).all()
    # Most recent short error (class name + code, never upstream text) per failed group.
    errors: dict[tuple[str, JobStatus], str] = {}
    failed = await session.execute(
        select(BackfillJob.data_type, BackfillJob.status, BackfillJob.last_error)
        .where(
            BackfillJob.status.in_([JobStatus.failed, JobStatus.abandoned]),
            BackfillJob.last_error.is_not(None),
        )
        .order_by(BackfillJob.updated_at.desc())
    )
    for data_type, job_status, error in failed:
        errors.setdefault((data_type, job_status), error)
    return [
        {
            "data_type": row[0],
            "status": row[1],
            "windows": row[2],
            "records": row[3] or 0,
            "last_error": errors.get((row[0], row[1])),
        }
        for row in rows
    ]
