from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.db.session import get_session
from app.services.devices import Thresholds, device_report
from app.services.runtime import Runtime
from app.services.source_precedence import Policy

router = APIRouter(tags=["devices"])


@router.get("/api/devices")
async def devices(
    response: Response,
    session: AsyncSession = Depends(get_session),
    runtime: Runtime = Depends(runtime_dep),
    settings: Settings = Depends(settings_dep),
) -> dict:
    """Cihazlarım: every device the archive has seen, with its freshness status; Google's
    paired-device sync times when Google answers (``google: unavailable`` otherwise)."""
    response.headers["Cache-Control"] = "no-store"
    return await device_report(
        session,
        paired_devices=runtime.discovery.devices,
        thresholds=Thresholds(settings.devices_connected_hours, settings.devices_delayed_days),
        policy=Policy.from_settings(settings),
        garmin_name=settings.garmin_device_name,
    )
