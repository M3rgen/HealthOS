"""Pipeline watchdog test helpers: a health stub, a reconciliation stub and the watchdog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.services.synchronization import reconciliation_reports as reports
from app.services.synchronization.pipeline_health import PipelineSnapshot, PipelineStatus
from app.sync.watchdog import PipelineWatchdog


class Health:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def inspect(self, **kwargs):
        return self.snapshot


class Reconciliation:
    running = False

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"steps": object()}

    async def run(self, days, *, run_id):
        self.calls.append(days)
        now = datetime.now(UTC)
        await reports.begin_run(
            self.sessions, run_id, now - timedelta(days=days), now, now, self.result
        )
        for data_type, result in self.result.items():
            await reports.start_type(self.sessions, run_id, data_type)
            await reports.finish_type(
                self.sessions, run_id, data_type, result if isinstance(result, Exception) else None
            )
        await reports.finish_run(self.sessions, run_id)
        return self.result


def _snapshot(now, status=PipelineStatus.stale, age=timedelta(hours=3)):
    return PipelineSnapshot(
        status=status,
        checked_at=now,
        last_webhook_received=None,
        last_webhook_processed=None,
        last_sync_success=now - age,
        latest_frequent_sync=now - age,
        latest_health_write=None,
        last_reconciliation=None,
        pending_notifications=0,
        failed_notifications=0,
        failed_syncs=0,
    )


def _watchdog(sessions, now, reconciliation=None, **settings):
    config = Settings(
        _env_file=None,
        startup_gap_threshold_minutes=120,
        recovery_cooldown_minutes=360,
        **settings,
    )
    reconciliation = reconciliation or Reconciliation()
    reconciliation.sessions = sessions
    return (
        PipelineWatchdog(config, sessions, Health(_snapshot(now)), reconciliation),
        reconciliation,
    )
