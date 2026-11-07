from __future__ import annotations

import logging

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobEvent,
    JobExecutionEvent,
)

from app.core.config import Settings
from app.core.logging import event
from app.services.derived import DerivedRefresh
from app.services.garmin_pull import GarminPull, cron_fields
from app.services.identity_reconciler import IdentityReconciler
from app.services.pipeline_health import PipelineHealthService
from app.services.scheduler_shutdown import AppScheduler
from app.services.watchdog import PipelineWatchdog
from app.sync.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

# A blocked event loop (e.g. hashing a large record batch) must not silently drop the
# daily reconciliation: run a late job within an hour, and collapse missed runs to one.
JOB_DEFAULTS = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}


def log_missed_job(scheduled: JobExecutionEvent) -> None:
    event(
        logger,
        "SCHEDULER_JOB_MISSED",
        job_id=scheduled.job_id,
        scheduled_run_time=str(scheduled.scheduled_run_time),
    )


RECONCILIATION_JOBS = frozenset({"daily-reconciliation", "weekly-reconciliation"})


def chain_identity(scheduler: AppScheduler, identity: IdentityReconciler) -> None:
    """Start the identity reconciler once no scheduled Google reconciliation is in flight.

    A reconciliation holds each data type's shared lock while it stores and checks deletions
    (heart-rate for up to an hour of the 7-day run), so a reconciler started at a fixed time
    met it night after night (issue #262). Each daily or weekly run that ends (or fails, or
    is missed) starts it, unless another one is still running or queued behind it."""
    in_flight: set[str] = set()

    def listener(job: JobEvent) -> None:
        if job.job_id not in RECONCILIATION_JOBS:
            return
        if job.code == EVENT_JOB_SUBMITTED:
            in_flight.add(job.job_id)
            return
        in_flight.discard(job.job_id)
        if in_flight:
            return
        event(logger, "IDENTITY_RECONCILE_CHAINED", after=job.job_id)
        scheduler.add_job(
            identity.run, "date", id="identity-reconcile", replace_existing=True, **JOB_DEFAULTS
        )

    scheduler.add_listener(
        listener, EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
    )


def build_scheduler(
    settings: Settings,
    reconciliation: ReconciliationService,
    watchdog: PipelineWatchdog | None = None,
    pipeline_health: PipelineHealthService | None = None,
    identity: IdentityReconciler | None = None,
    derived: DerivedRefresh | None = None,
    garmin: GarminPull | None = None,
) -> AppScheduler:
    scheduler = AppScheduler(timezone="UTC")
    scheduler.add_listener(log_missed_job, EVENT_JOB_MISSED)
    if settings.reconciliation_enabled:
        scheduler.add_job(
            reconciliation.run,
            "cron",
            hour=settings.reconciliation_daily_hour_utc,
            kwargs={"days": settings.reconciliation_daily_days},
            id="daily-reconciliation",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
        scheduler.add_job(
            reconciliation.run,
            "cron",
            day_of_week=settings.reconciliation_weekly_day,
            hour=settings.reconciliation_weekly_hour_utc,
            kwargs={"days": settings.reconciliation_weekly_days},
            id="weekly-reconciliation",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
    if settings.watchdog_enabled and watchdog is not None:
        scheduler.add_job(
            watchdog.periodic_check,
            "interval",
            seconds=settings.watchdog_interval_seconds,
            id="pipeline-watchdog",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
    if settings.identity_reconcile_enabled and identity is not None:
        # The only place that decides renamed sources and links duplicate rows between
        # deploys: incremental, from the last checkpoint (app/services/identity_backfill.py).
        if settings.reconciliation_enabled:
            chain_identity(scheduler, identity)
        else:
            scheduler.add_job(
                identity.run,
                "cron",
                hour=settings.identity_reconcile_hour_utc,
                minute=settings.identity_reconcile_minute,
                id="identity-reconcile",
                replace_existing=True,
                **JOB_DEFAULTS,
            )
    if pipeline_health is not None:
        # Hourly slot: the only place (besides an expired-cache fallback) that reads
        # raw_health_data for the /health freshness summary.
        scheduler.add_job(
            pipeline_health.refresh_freshness,
            "cron",
            minute=0,
            id="pipeline-freshness-refresh",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
    if derived is not None:
        scheduler.add_job(
            derived.run,
            "interval",
            minutes=5,
            id="derived-refresh",
            replace_existing=True,
            **JOB_DEFAULTS,
        )
    if garmin is not None and garmin.enabled:
        # Read-only pull of the last GARMIN_LOOKBACK_DAYS from Garmin, outside the nightly
        # 02:00-03:59 UTC window (reconciliation, identity reconciler, backup); a failed kind
        # is logged and recorded in garmin_pull_state, the next run fetches the window again.
        scheduler.add_job(
            garmin.run,
            "cron",
            id="garmin-pull",
            replace_existing=True,
            **cron_fields(settings.garmin_pull_interval_minutes),
            **JOB_DEFAULTS,
        )
    return scheduler
