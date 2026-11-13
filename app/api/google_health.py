from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import runtime as runtime_dep
from app.google_health.client import GoogleHealthError
from app.google_health.oauth import OAuthError
from app.runtime import Runtime
from app.schemas.status import Capabilities, GoogleDevices

router = APIRouter(prefix="/google-health", tags=["google-health"])


@router.get("/capabilities", response_model=Capabilities)
async def capabilities(
    days: int = Query(30, ge=1, le=365), runtime: Runtime = Depends(runtime_dep)
) -> Capabilities:
    try:
        return {
            "lookback_days": days,
            "data_types": await runtime.discovery.probe(days),
        }
    except (OAuthError, GoogleHealthError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/devices", response_model=GoogleDevices)
async def devices(runtime: Runtime = Depends(runtime_dep)) -> GoogleDevices:
    try:
        return {"devices": await runtime.discovery.devices()}
    except (OAuthError, GoogleHealthError) as exc:
        raise HTTPException(503, str(exc)) from exc
