# Keep dependency annotations concrete when FastAPI re-inspects them for overrides.

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ManualWriteRoute, require_job_request
from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.data_types import supported_data_types
from app.runtime import Runtime
from app.schemas.status import Accepted, Coverage, ReconciliationHistory, SyncStatus
from app.services.shared.query_ranges import CivilDateRange
from app.services.synchronization.reconciliation_reports import list_runs
from app.services.synchronization.retrieval_coverage import MAX_DAYS, CoverageLimit, coverage_report
from app.services.synchronization.sync_status import sync_status, sync_statuses

router = APIRouter(prefix="/sync", tags=["sync"], route_class=ManualWriteRoute)
coverage_router = APIRouter(tags=["sync"])


@coverage_router.get("/api/sync/reconciliation", response_model=ReconciliationHistory)
async def reconciliation_history(
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> ReconciliationHistory:
    return await list_runs(session, offset, limit)


@dataclass(frozen=True)
class _CoverageQuery:
    start_date: date
    end_date: date
    timezone: str = Query(..., min_length=1, max_length=100)
    data_type: str | None = Query(None, max_length=100)
    offset: int = Query(0, ge=0, le=1000)
    limit: int = Query(4, ge=1, le=4)


@coverage_router.get("/api/sync/coverage", response_model=Coverage)
async def coverage(
    query: _CoverageQuery = Depends(_CoverageQuery),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(settings_dep),
) -> Coverage:
    specs = supported_data_types(settings.extra_data_types)
    if (
        not 1 <= (query.end_date - query.start_date).days + 1 <= MAX_DAYS
        or query.start_date < date.min + timedelta(days=3)
        or query.end_date > date.max - timedelta(days=3)
    ):
        raise HTTPException(422, {"code": "coverage_invalid_range", "max_days": MAX_DAYS})
    try:
        ZoneInfo(query.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, {"code": "coverage_invalid_timezone"}) from None
    if query.data_type is not None and query.data_type not in specs:
        raise HTTPException(422, {"code": "coverage_unknown_data_type", "accepted": sorted(specs)})
    try:
        return await coverage_report(
            session,
            specs,
            query.data_type,
            query.offset,
            query.limit,
            dates=CivilDateRange(query.start_date, query.end_date, query.timezone),
        )
    except CoverageLimit as exc:
        raise HTTPException(
            413, {"code": str(exc), "hint": "Tarih aralığını veya limit değerini azalt."}
        ) from None


@router.get("/status", response_model=list[SyncStatus])
async def all_status(
    session: AsyncSession = Depends(get_session), runtime: Runtime = Depends(runtime_dep)
) -> list[SyncStatus]:
    return await sync_statuses(session, runtime)


@router.get("/status/{data_type}", response_model=SyncStatus)
async def one_status(
    data_type: str,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(runtime_dep),
) -> SyncStatus:
    row = await sync_status(session, data_type, runtime)
    if row is None:
        raise HTTPException(404, "No synchronization state for this data type")
    return row


@router.post(
    "/all",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_job_request)],
    response_model=Accepted,
)
async def start_all(runtime: Runtime = Depends(runtime_dep)) -> Accepted:
    job = runtime.sync_jobs.start(None, runtime.incremental, runtime.spawn)
    return {
        "status": "accepted",
        "data_types": list(runtime.incremental.specs),
        "sequence": job.sequence,
        "job_id": job.job_id,
    }


@router.post(
    "/{data_type}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_job_request)],
    response_model=Accepted,
)
async def start_one(data_type: str, runtime: Runtime = Depends(runtime_dep)) -> Accepted:
    if data_type not in runtime.incremental.specs:
        raise HTTPException(404, "Unsupported data type")
    job = runtime.sync_jobs.start(data_type, runtime.incremental, runtime.spawn)
    return {"status": "accepted", "data_type": data_type, "job_id": job.job_id}
