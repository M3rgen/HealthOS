"""The synthetic sample data from the interface: is it loaded, load it, remove it.

Behind the normal UI login like every ``/api`` route; the two writes also need the deliberate
request header (CSRF) and share the manual write budget. No source credentials are involved.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_budget, require_job_request
from app.db.session import get_session
from app.schemas.demo import SampleLoad, SampleRemoval, SampleStatus
from app.services.providers.demo_archive import (
    SampleRefused,
    load_sample,
    remove_sample,
    sample_status,
)

router = APIRouter(prefix="/api/demo", tags=["sample data"])


@router.get("", response_model=SampleStatus)
async def status(response: Response, session: AsyncSession = Depends(get_session)) -> SampleStatus:
    """Whether the synthetic sample data is loaded, its row count and the days it covers."""
    response.headers["Cache-Control"] = "no-store"
    return await sample_status(session)


@router.post("", response_model=SampleLoad, dependencies=[Depends(require_job_request)])
async def load(
    request: Request,
    timezone: str = Query("UTC", max_length=64, description="IANA time zone of the days."),
    session: AsyncSession = Depends(get_session),
) -> SampleLoad:
    """Load about 90 days of synthetic sample data into an archive with no device data."""
    require_budget(request.app.state.write_limits.manual)
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, "Saat dilimi geçersiz.") from exc
    try:
        added = await load_sample(session, zone, datetime.now(UTC))
    except SampleRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return {"added": added, **(await sample_status(session))}


@router.delete("", response_model=SampleRemoval, dependencies=[Depends(require_job_request)])
async def remove(request: Request, session: AsyncSession = Depends(get_session)) -> SampleRemoval:
    """Delete the synthetic sample data, and nothing else."""
    require_budget(request.app.state.write_limits.manual)
    try:
        removed = await remove_sample(session)
    except SampleRefused as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.commit()
    return {"removed": removed, **(await sample_status(session))}
