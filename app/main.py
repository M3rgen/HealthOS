from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from functools import partial
from os import PathLike, stat_result
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.types import Scope

from app.api import (
    apple,
    auth,
    backfill,
    codex,
    dashboard,
    demo,
    devices,
    fit_import,
    garmin,
    google_health,
    health,
    login,
    sync,
    webhooks,
)
from app.api import settings as settings_api
from app.core.auth import SessionStore, UITokenMiddleware
from app.core.canonical_host import CanonicalHostMiddleware, redirect_enabled
from app.core.config import Settings, get_settings
from app.core.host_validation import HostValidationMiddleware
from app.core.logging import configure_logging, event
from app.core.oauth_state import OAuthStateStore
from app.core.request_limits import WriteLimits
from app.core.role_policy import (
    PRIVILEGED_MIGRATION_STEP,
    SuperuserRuntimeRefused,
    superuser_grace,
)
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.sources import garmin_key_mode, not_installed_sources, permissive_key_mode
from app.core.version import app_version
from app.db.role_check import PRIVILEGED_ROLE, SUPERUSER, database_role_risk
from app.db.session import SessionLocal, database_lifespan
from app.runtime import Runtime, build_runtime
from app.services.synchronization.scheduler import SchedulerServices, build_scheduler
from app.sync.incremental import STALE_RUNNING_AFTER

logger = logging.getLogger(__name__)
# body.d736859b.glb: a content-hashed name never changes meaning, so it can be cached
# forever; anything else under /models must be revalidated.
HASHED_NAME = re.compile(r"[^/]+[.-][0-9a-f]{8,64}\.[A-Za-z0-9]+")


class ModelFiles(StaticFiles):
    def file_response(
        self,
        full_path: str | PathLike[str],
        stat_result: stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        hashed = HASHED_NAME.fullmatch(Path(full_path).name)
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if hashed else "no-cache"
        )
        return response


async def recover_stale_running(runtime: Runtime, delay: timedelta = timedelta(0)) -> None:
    if delay:
        # A little past the threshold, so a row last touched just before startup is stale.
        await asyncio.sleep(delay.total_seconds() + 60)
    await runtime.incremental.recover_interrupted()
    await runtime.webhook_processor.recover_running()


def gate_database_role(state: Any, settings: Settings, today: date | None = None) -> None:
    """Refuse a privileged login in production unless a dated opt-in still covers today.

    The schema owner without superuser stays a warning: it can still disable the guards of the
    tables it owns, which ``provision-app-role`` takes away once the operator opts in.
    """
    risk = getattr(state, "database_role_risk", None)
    if risk in {SUPERUSER, PRIVILEGED_ROLE}:
        kind = "PRIVILEGED" if risk == PRIVILEGED_ROLE else "SUPERUSER"
        try:
            until = superuser_grace(
                settings.healthos_allow_superuser_until,
                today or datetime.now(UTC).date(),
                privileged=risk == PRIVILEGED_ROLE,
            )
        except SuperuserRuntimeRefused as refused:
            # The message itself: the log filter keeps exception text out of every record.
            logger.error("%s", refused, extra={"event": f"DATABASE_ROLE_{kind}_REFUSED"})
            raise
        state.database_role_grace_until = until
        description = "a privileged role" if risk == PRIVILEGED_ROLE else "a superuser"
        step = (
            PRIVILEGED_MIGRATION_STEP
            if risk == PRIVILEGED_ROLE
            else "Set POSTGRES_APP_USER and POSTGRES_APP_PASSWORD (docs/DEPLOYMENT.md)."
        )
        logger.warning(
            f"The application connects to PostgreSQL as {description} until %s "
            f"(HEALTHOS_ALLOW_SUPERUSER_UNTIL); after that day it refuses to start. {step}",
            until.isoformat(),
            extra={"event": f"DATABASE_ROLE_{kind}_GRACE"},
        )
    elif risk:
        logger.warning(
            "The application connects to PostgreSQL as %s: an SQL bug could reach "
            "what that role can do. Set POSTGRES_APP_USER and POSTGRES_APP_PASSWORD "
            "(docs/RUNBOOK.md).",
            risk,
            extra={"event": "DATABASE_ROLE_UNRESTRICTED"},
        )


def warn_ignored_google_roots(settings: Settings) -> None:
    if settings.google_token_encryption_keys_old:
        logger.warning(
            "GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD is set but no longer read: "
            "retired roots never decrypt stored Google tokens; "
            "a rotated or restored root needs a new Google consent.",
            extra={
                "event": "GOOGLE_TOKEN_KEYS_OLD_IGNORED",
                "count": len(settings.google_token_encryption_keys_old),
            },
        )


def warn_garmin_key_permissions(settings: Settings) -> None:
    if settings.garmin_enabled and permissive_key_mode(
        mode := garmin_key_mode(settings.garmin_api_key_file)
    ):
        mode_text = f"{mode:o}"
        logger.warning(
            "The Garmin API key file is readable by group or others: "
            "expected mode 600 or 400, found %s. A future release will refuse "
            "to start; run chmod 600 on the key file.",
            mode_text,
            extra={"event": "GARMIN_KEY_FILE_PERMISSIVE", "mode": mode_text},
        )


@asynccontextmanager
async def _application_lifespan(app: FastAPI, settings: Settings, runtime: Runtime):
    async with database_lifespan(settings, SessionLocal):
        scheduler = None
        try:
            event(logger, "APPLICATION_STARTING")
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
                if settings.production:
                    app.state.database_role_risk = await database_role_risk(session)
            event(logger, "DATABASE_READY")
            gate_database_role(app.state, settings)
            warn_ignored_google_roots(settings)
            warn_garmin_key_permissions(settings)
            for source in not_installed_sources(settings.healthos_sources):
                logger.warning(
                    "HEALTHOS_SOURCES selects %s, but its importer is not installed here: "
                    "nothing is ingested for it",
                    source,
                    extra={"event": "SOURCE_IMPORTER_NOT_INSTALLED", "source": source},
                )
            if settings.google_enabled:
                await runtime.webhook_status.start()
                # Recover Google jobs only while that source is selected; keep archived state
                # untouched in archive-only mode, including interrupted work.
                await recover_stale_running(runtime)
                if settings.app_env != "test":
                    runtime.spawn(recover_stale_running(runtime, delay=STALE_RUNNING_AFTER))
            if runtime.scheduler_needed(settings):
                scheduler = build_scheduler(
                    settings,
                    runtime.reconciliation,
                    services=SchedulerServices(
                        watchdog=runtime.watchdog,
                        pipeline_health=runtime.pipeline_health,
                        identity=runtime.identity,
                        derived=runtime.derived,
                        garmin=runtime.garmin,
                        webhooks=runtime.webhook_processor,
                    ),
                )
                scheduler.start()
                runtime.scheduler = scheduler
                event(
                    logger,
                    "SCHEDULER_STARTED",
                    reconciliation_enabled=settings.reconciliation_enabled,
                    watchdog_enabled=settings.watchdog_enabled,
                    identity_reconcile_enabled=settings.identity_reconcile_enabled,
                    garmin_enabled=settings.garmin_enabled,
                )
                if (
                    settings.google_enabled or settings.garmin_enabled
                ) and settings.watchdog_enabled:
                    runtime.spawn(runtime.watchdog.startup_check())
            event(logger, "APPLICATION_READY")
            yield
            event(logger, "APPLICATION_STOPPING")
        finally:
            try:
                if scheduler:
                    await scheduler.shutdown_and_wait()
                    runtime.scheduler = None
            finally:
                await runtime.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings, SessionLocal)

    # The interactive docs need inline scripts and the jsDelivr CDN (see
    # security_headers.DOCS_POLICY); they are a development tool, so production, where
    # the login cookie lives, serves none of them.
    docs = not settings.production
    application = FastAPI(
        title="HealthOS",
        version=app_version(),
        description="Read-only, raw-first archive of your own health data",
        lifespan=partial(_application_lifespan, settings=settings, runtime=runtime),
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    application.state.settings = settings
    application.state.runtime = runtime
    application.state.write_limits = WriteLimits(settings)
    # Apple Health (app/api/apple.py): the receiver's own request budgets and the session
    # factory its imports write through.
    application.state.apple_limits = apple.AppleLimits(settings)
    application.state.apple_sessions = SessionLocal
    # Login sessions (in memory; a restart logs the browser out).
    application.state.ui_sessions = ui_sessions = SessionStore()
    application.state.oauth_states = OAuthStateStore()
    # Starlette runs the last-added middleware first: headers, host check, the move from plain
    # localhost to healthos.localhost (before a login could set a cookie there), UI token.
    application.add_middleware(UITokenMiddleware, token=settings.app_ui_token, sessions=ui_sessions)
    application.add_middleware(
        CanonicalHostMiddleware, enabled=redirect_enabled(settings.app_public_url)
    )
    application.add_middleware(
        HostValidationMiddleware,
        webhook_endpoint_uri=(
            settings.google_health_webhook_endpoint_uri if settings.google_enabled else None
        ),
        apple_endpoint_uri=settings.apple_receiver_endpoint_uri,
    )
    application.add_middleware(SecurityHeadersMiddleware, docs_enabled=docs)
    application.include_router(health.router)
    application.include_router(login.router)
    if settings.google_enabled:
        for router in (
            auth.router,
            sync.router,
            sync.coverage_router,
            backfill.router,
            google_health.router,
            webhooks.router,
        ):
            application.include_router(router)
    application.include_router(fit_import.router)
    application.include_router(dashboard.router)
    application.include_router(codex.router)
    application.include_router(settings_api.router)
    application.include_router(devices.router)
    application.include_router(demo.router)
    if settings.garmin_enabled:
        application.include_router(garmin.router)
    application.include_router(apple.router)
    static = Path(__file__).parent / "static"
    if static.is_dir():
        application.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")
        if (static / "models").is_dir():
            application.mount("/models", ModelFiles(directory=static / "models"), name="models")

        @application.get("/", include_in_schema=False)
        async def index():
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    return application


app = create_app()
