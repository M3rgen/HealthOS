"""Archive synchronization reports consumed by the CLI."""

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState


async def sync_status(session: AsyncSession) -> dict:
    sync_rows = (await session.scalars(select(SyncState).order_by(SyncState.data_type))).all()
    raw_count = await session.scalar(select(func.count(RawHealthData.id)))
    jobs = await session.execute(
        select(BackfillJob.status, func.count()).group_by(BackfillJob.status)
    )
    from app.models.identity_tables import IdentityReconcileState
    from app.services.archive.source_deletions import deferred_deletions
    from app.services.identities.identity_reconcile_window import failed_nights
    from app.services.identities.identity_schema import identity_tables_missing

    identity = []
    open_chains: list[dict] = []
    deferred: list[dict] = []
    if not await identity_tables_missing(session):
        identity = (
            await session.scalars(
                select(IdentityReconcileState).order_by(IdentityReconcileState.data_type)
            )
        ).all()
        open_chains = await _chains_to_review(session)
        deferred = await deferred_deletions(session)
    return {
        "raw_records": raw_count,
        "sync": [
            {
                "data_type": row.data_type,
                "status": row.status.value,
                "last_successful_sync": str(row.last_successful_sync),
            }
            for row in sync_rows
        ],
        "backfill": {row[0].value: row[1] for row in jobs},
        # The identity reconciler (identity-backfill; nightly --incremental).
        "identity_reconcile": [
            {
                "data_type": row.data_type,
                "full_run_at": str(row.full_run_at) if row.full_run_at else None,
                "checkpoint_at": str(row.checkpoint_at) if row.checkpoint_at else None,
                "last_finished_at": (str(row.last_finished_at) if row.last_finished_at else None),
                "last_mode": row.last_mode,
                "last_result": json.loads(row.last_result) if row.last_result else None,
                "last_error": row.last_error,
                # Nights in a row without a successful run (null: the last run did).
                "failed_nights": failed_nights(row),
            }
            for row in identity
        ],
        # Chains the reconciler decided that the owner has not confirmed or rejected
        # yet (healthos identity-decide confirm|reject).
        "identity_review_suggested": open_chains,
        # Deletions complete fetches left undecided: a source that may be the
        # record's device renamed returned its reading (identity-decide).
        "identity_deferred_deletions": deferred,
    }


async def _chains_to_review(session: AsyncSession) -> list[dict]:
    from app.models.identity_tables import RawSourceChain
    from app.services.identities.source_chain_repository import load_chains

    found = []
    names = (await session.scalars(select(RawSourceChain.data_type).distinct())).all()
    for data_type in sorted(names):
        chains = await load_chains(session, data_type)
        for old, new in sorted(chains.links):
            if chains.review(old, new) == "suggested" and chains.live(old, new):
                found.append(
                    {
                        "data_type": data_type,
                        "from": chains.spans[old].label if old in chains.spans else old[:12],
                        "to": chains.spans[new].label if new in chains.spans else new[:12],
                        "fingerprints": [old[:12], new[:12]],
                        "decided_by": chains.decided_by.get((old, new)),
                    }
                )
    return found


async def failed_backfills(session: AsyncSession, data_type: str | None) -> list[dict]:
    query = select(BackfillJob).where(
        BackfillJob.status.in_([JobStatus.failed, JobStatus.abandoned])
    )
    if data_type:
        query = query.where(BackfillJob.data_type == data_type)
    return [
        {
            "status": row.status.value,
            "data_type": row.data_type,
            "window_start": row.window_start,
            "last_error": row.last_error,
        }
        for row in (await session.scalars(query)).all()
    ]
