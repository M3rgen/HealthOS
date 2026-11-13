from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ManualWriteRoute, require_job_request
from app.api.deps import runtime as runtime_dep
from app.db.session import get_session
from app.runtime import Runtime
from app.schemas.status import Accepted, BackfillStatus
from app.services.synchronization.sync_status import backfill_status

router = APIRouter(prefix="/backfill", tags=["backfill"], route_class=ManualWriteRoute)


@router.post(
    "/start",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_job_request)],
    response_model=Accepted,
)
async def start(
    data_type: str | None = Query(None), runtime: Runtime = Depends(runtime_dep)
) -> Accepted:
    if data_type and data_type not in runtime.backfill.specs:
        raise HTTPException(404, "Unsupported data type")
    # A request while this selection runs joins that run
    # (app/services/synchronization/sync_jobs.py).
    runtime.backfill_jobs.start(data_type or None, runtime.backfill, runtime.spawn)
    selected = [data_type] if data_type else list(runtime.backfill.specs)
    return {"status": "accepted", "data_types": selected}


@router.get("/status", response_model=list[BackfillStatus])
async def status_endpoint(session: AsyncSession = Depends(get_session)) -> list[BackfillStatus]:
    return await backfill_status(session)
