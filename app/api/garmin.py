from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import runtime as runtime_dep
from app.services.runtime import Runtime

router = APIRouter(prefix="/api/garmin", tags=["garmin"])
# One request covers at most a quarter: the summary is read from the newest record per day.
MAX_DAYS = 92


@router.get("/status")
async def garmin_status(response: Response, runtime: Runtime = Depends(runtime_dep)) -> dict:
    """Pull state of the Garmin Connect Personal API archive (no values, no secrets)."""
    response.headers["Cache-Control"] = "no-store"
    if runtime.garmin is None:
        return {"enabled": False, "key_file_present": False, "kinds": {}}
    try:
        return await runtime.garmin.status()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Garmin status unavailable") from exc


@router.get("/daily")
async def garmin_daily(
    response: Response,
    start: date = Query(..., alias="from"),
    end: date = Query(..., alias="to"),
    runtime: Runtime = Depends(runtime_dep),
) -> dict:
    """Steps, resting heart rate, sleep, body battery and stress per day, from Garmin only."""
    response.headers["Cache-Control"] = "no-store"
    if not 1 <= (end - start).days + 1 <= MAX_DAYS:
        raise HTTPException(422, {"code": "garmin_invalid_range", "max_days": MAX_DAYS})
    if runtime.garmin is None:
        return {"source": "garmin", "from": start.isoformat(), "to": end.isoformat(), "days": []}
    try:
        return await runtime.garmin.daily(start, end)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Garmin archive unavailable") from exc
