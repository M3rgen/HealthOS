from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import Settings
from app.core.logging import event
from app.core.sources import garmin_key_mode, not_installed_sources, permissive_key_mode
from app.runtime import Runtime
from app.schemas.status import Health, Live
from app.services.operations.backup_status import BackupStatus, backup_reasons, read_backup_status

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health/backup", response_model=BackupStatus)
def backup(request: Request) -> BackupStatus:
    return read_backup_status(request.app.state.settings.backup_status_file)


@router.get("/health/live", response_model=Live)
@router.head("/health/live", include_in_schema=False, response_model=Live)
async def live() -> Live:
    """Process liveness for container healthchecks; never touches the database."""
    return {"status": "ok", "service": "healthos"}


@router.get("/api/status", response_model=Health)
@router.get("/health", response_model=Health)
async def health(request: Request) -> Health | JSONResponse:
    runtime: Runtime = request.app.state.runtime
    settings: Settings = request.app.state.settings
    scheduler_expected = runtime.scheduler_needed(settings)
    scheduler_status = (
        "running"
        if runtime.scheduler_running
        else "disabled"
        if not scheduler_expected
        else "stopped"
    )
    configuration_status = (
        "healthy"
        if not settings.google_enabled
        or (
            settings.credentials_configured
            and settings.webhook_readiness["external_registration_ready"]
        )
        else "degraded"
    )
    try:
        snapshot = await runtime.pipeline_health.inspect(
            scheduler_running=not scheduler_expected or runtime.scheduler_running
        )
    except Exception as exc:
        event(logger, "HEALTH_DATABASE_UNAVAILABLE", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "active_sources": settings.healthos_sources,
                "status": "failed",
                "database": "unavailable",
                "scheduler": scheduler_status,
                "configuration": configuration_status,
                "service": "healthos",
                "ui_login_disabled": not bool(settings.app_ui_token),
            },
        )
    payload = snapshot.as_dict(
        database="healthy",
        scheduler=scheduler_status,
        configuration=configuration_status,
    )
    if configuration_status != "healthy" and payload["status"] == "healthy":
        payload["status"] = "degraded"
        payload["reasons"].append("configuration_incomplete")
    for source in not_installed_sources(settings.healthos_sources):
        payload["reasons"].append(f"{source}_importer_not_installed")
    if settings.garmin_enabled and permissive_key_mode(
        garmin_key_mode(settings.garmin_api_key_file)
    ):
        payload["reasons"].append("garmin_key_file_permissive")
    role_risk = getattr(request.app.state, "database_role_risk", None)
    if role_risk:
        # A reason, not a degraded status: nothing is failing, the operator should know.
        payload["reasons"].append(f"database_role_{role_risk}")
        if getattr(request.app.state, "database_role_grace_until", None):
            # Running only because HEALTHOS_ALLOW_SUPERUSER_UNTIL still covers today.
            payload["reasons"].append(f"database_role_{role_risk}_grace")
    backup_state = await asyncio.to_thread(read_backup_status, settings.backup_status_file)
    # Reasons only, like the role risk above: a new install has no backup yet, and monitors and
    # the interface read `status`, which must keep meaning that the service itself is working.
    payload["reasons"].extend(backup_reasons(backup_state))
    return {
        **payload,
        "last_backup_success": backup_state["last_success_at"],
        "active_sources": settings.healthos_sources,
        "service": "healthos",
        "ui_login_disabled": not bool(settings.app_ui_token),
        "watchdog": "enabled"
        if (settings.google_enabled or settings.garmin_enabled) and settings.watchdog_enabled
        else "disabled",
    }
