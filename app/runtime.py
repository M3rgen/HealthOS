from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.logging import error_diagnostics
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import mirror_data_types, supported_data_types
from app.google_health.oauth import OAuthClient, TokenProvider
from app.google_health.webhook import WebhookSignatureVerifier
from app.services.archive.responses import DatabaseResponseArchive
from app.services.identities.identity_reconciler import IdentityReconciler
from app.services.interpretation.derived import DerivedRefresh
from app.services.providers.credentials import CredentialStore
from app.services.providers.discovery import DiscoveryService
from app.services.providers.garmin_pull import GarminPull
from app.services.providers.ingestion import GoogleSource
from app.services.synchronization.pipeline_health import PipelineHealthService
from app.services.synchronization.scheduler_shutdown import AppScheduler
from app.services.synchronization.sync_jobs import BackfillJobs, SyncJobs
from app.services.synchronization.webhook_status import WebhookStatusService
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync
from app.sync.ownership import garmin_ownership
from app.sync.reconciliation import ReconciliationService
from app.sync.watchdog import PipelineWatchdog
from app.sync.webhook_processing import WebhookBatchProcessor

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    credentials: CredentialStore
    oauth: OAuthClient
    tokens: TokenProvider
    google: GoogleHealthClient
    incremental: IncrementalSync
    backfill: BackfillService
    reconciliation: ReconciliationService
    webhook_verifier: WebhookSignatureVerifier
    webhook_processor: WebhookBatchProcessor
    discovery: DiscoveryService
    pipeline_health: PipelineHealthService
    watchdog: PipelineWatchdog
    webhook_status: WebhookStatusService
    derived: DerivedRefresh | None = None
    identity: IdentityReconciler | None = None
    # Garmin Connect Personal API pull; present when GARMIN_ENABLED
    # (app/services/providers/garmin_pull.py).
    garmin: GarminPull | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)
    scheduler: AppScheduler | None = None
    sync_jobs: SyncJobs = field(default_factory=SyncJobs)
    backfill_jobs: BackfillJobs = field(default_factory=BackfillJobs)

    @property
    def scheduler_running(self) -> bool:
        return bool(self.scheduler and self.scheduler.running)

    def scheduler_needed(self, settings: Settings) -> bool:
        """Whether the app starts APScheduler, so /health can tell `stopped` from `disabled`.

        The derived-day refresh is always wired, so outside tests the scheduler runs even when
        reconciliation, the watchdog and the identity reconciler are all switched off.
        """
        return settings.app_env != "test" and (
            (
                settings.google_enabled
                and (
                    settings.reconciliation_enabled
                    or settings.watchdog_enabled
                    or settings.identity_reconcile_enabled
                )
            )
            or settings.garmin_enabled
            or self.derived is not None
        )

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            # Where it failed (module, function, line) without its message or locals.
            logger.error(
                "Background job failed",
                extra={
                    "event": "BACKGROUND_JOB_FAILED",
                    "error_type": type(error).__name__,
                    **error_diagnostics(error, frames=True),
                },
            )

    async def shutdown(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.oauth.http.aclose()
        await self.google.http.aclose()
        await self.webhook_verifier.http.aclose()
        if self.garmin is not None:
            await self.garmin.aclose()


def build_runtime(settings: Settings, sessions: async_sessionmaker) -> Runtime:
    credentials = CredentialStore(settings, sessions)
    oauth = OAuthClient(settings)
    tokens = TokenProvider(
        oauth,
        credentials.load_refresh_token,
        credentials.save_refresh_token,
        credential_validator=credentials.account_is_current,
    )
    google = GoogleHealthClient(settings, tokens, archive=DatabaseResponseArchive(sessions))
    discovery_specs = supported_data_types(settings.extra_data_types, settings.enabled_data_types)
    mirror_specs = mirror_data_types(settings.extra_data_types, settings.enabled_data_types)
    source = GoogleSource(google)
    incremental = IncrementalSync(settings, sessions, source, mirror_specs)
    backfill = BackfillService(settings, sessions, source, mirror_specs)
    reconciliation = ReconciliationService(incremental, sessions)
    frequent_types = {
        "heart-rate",
        "activity-level",
        "steps",
        "distance",
        "active-energy-burned",
        "total-calories",
        "active-minutes",
    }.intersection(mirror_specs)
    pipeline_health = PipelineHealthService(settings, sessions, frequent_types)
    watchdog = PipelineWatchdog(settings, sessions, pipeline_health, reconciliation)
    webhook_status = WebhookStatusService(settings, sessions)
    return Runtime(
        credentials=credentials,
        oauth=oauth,
        tokens=tokens,
        google=google,
        incremental=incremental,
        backfill=backfill,
        reconciliation=reconciliation,
        webhook_verifier=WebhookSignatureVerifier(settings.google_health_webhook_public_key_url),
        webhook_processor=WebhookBatchProcessor(
            sessions, incremental, set(mirror_specs), webhook_status
        ),
        webhook_status=webhook_status,
        discovery=DiscoveryService(google, discovery_specs, settings.sync_concurrency),
        pipeline_health=pipeline_health,
        watchdog=watchdog,
        identity=IdentityReconciler(
            sessions,
            time(
                settings.identity_reconcile_until_hour_utc, settings.identity_reconcile_until_minute
            ),
            settings.identity_reconcile_lock_wait_minutes,
        ),
        derived=DerivedRefresh(sessions),
        garmin=(
            GarminPull(settings, sessions).owned_by(garmin_ownership(sessions))
            if settings.garmin_enabled
            else None
        ),
    )
