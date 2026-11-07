import logging
from datetime import UTC, datetime

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

from app.core.config import Settings
from app.services.scheduler import build_scheduler, log_missed_job


def test_daily_and_weekly_reconciliation_schedule_is_configurable():
    class Reconciliation:
        async def run(self, days):
            return days

    scheduler = build_scheduler(
        Settings(
            reconciliation_enabled=True,
            reconciliation_daily_hour_utc=4,
            reconciliation_daily_days=8,
            reconciliation_weekly_day="mon",
            reconciliation_weekly_hour_utc=5,
            reconciliation_weekly_days=91,
        ),
        Reconciliation(),
    )

    daily = scheduler.get_job("daily-reconciliation")
    weekly = scheduler.get_job("weekly-reconciliation")
    assert daily.kwargs == {"days": 8}
    assert weekly.kwargs == {"days": 91}
    assert "hour='4'" in str(daily.trigger)
    assert "day_of_week='mon'" in str(weekly.trigger)
    assert "hour='5'" in str(weekly.trigger)


def test_watchdog_schedule_is_bounded_and_non_overlapping():
    class Reconciliation:
        async def run(self, days):
            return days

    class Watchdog:
        async def periodic_check(self):
            return None

    scheduler = build_scheduler(
        Settings(watchdog_enabled=True, watchdog_interval_seconds=600),
        Reconciliation(),
        Watchdog(),
    )
    job = scheduler.get_job("pipeline-watchdog")

    assert job.max_instances == 1
    assert "0:10:00" in str(job.trigger)


def test_freshness_summary_refreshes_in_hourly_slots():
    class Reconciliation:
        async def run(self, days):
            return days

    class Health:
        async def refresh_freshness(self):
            return None

    scheduler = build_scheduler(
        Settings(reconciliation_enabled=False, watchdog_enabled=True),
        Reconciliation(),
        None,
        Health(),
    )
    job = scheduler.get_job("pipeline-freshness-refresh")

    assert job.max_instances == 1
    assert "minute='0'" in str(job.trigger)


def test_every_job_tolerates_late_starts_and_coalesces_missed_runs():
    class Reconciliation:
        async def run(self, days):
            return days

    class Watchdog:
        async def periodic_check(self):
            return None

    class Health:
        async def refresh_freshness(self):
            return None

    class Identity:
        async def run(self):
            return None

    scheduler = build_scheduler(
        Settings(reconciliation_enabled=True, watchdog_enabled=True),
        Reconciliation(),
        Watchdog(),
        Health(),
        Identity(),
    )
    jobs = scheduler.get_jobs()

    # The identity reconciler is no cron job here: it follows the reconciliations (below).
    assert {job.id for job in jobs} == {
        "daily-reconciliation",
        "weekly-reconciliation",
        "pipeline-watchdog",
        "pipeline-freshness-refresh",
    }
    for job in jobs:
        assert job.misfire_grace_time == 3600
        assert job.coalesce is True
        assert job.max_instances == 1
    assert any(
        callback is log_missed_job and mask & EVENT_JOB_MISSED
        for callback, mask in scheduler._listeners
    )


def test_missed_job_is_logged_at_info(caplog, monkeypatch):
    # Alembic's fileConfig in the migration tests disables already-created loggers.
    monkeypatch.setattr(logging.getLogger("app.services.scheduler"), "disabled", False)
    with caplog.at_level(logging.INFO, logger="app.services.scheduler"):
        log_missed_job(
            JobExecutionEvent(
                EVENT_JOB_MISSED,
                "daily-reconciliation",
                "default",
                datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    [record] = [item for item in caplog.records if item.message == "SCHEDULER_JOB_MISSED"]
    assert record.levelno == logging.INFO
    assert record.job_id == "daily-reconciliation"


def test_identity_reconciler_keeps_a_fixed_time_only_without_reconciliation():
    class Reconciliation:
        async def run(self, days):
            return days

    class Identity:
        async def run(self):
            return None

    chained = build_scheduler(Settings(), Reconciliation(), identity=Identity())
    # Started by the end of the reconciliations (tests/test_identity_lock_window.py).
    assert chained.get_job("identity-reconcile") is None
    alone = Settings(reconciliation_enabled=False)
    default = build_scheduler(alone, Reconciliation(), identity=Identity())
    job = default.get_job("identity-reconcile")
    assert "hour='2'" in str(job.trigger) and "minute='30'" in str(job.trigger)
    moved = build_scheduler(
        Settings(
            reconciliation_enabled=False,
            identity_reconcile_hour_utc=4,
            identity_reconcile_minute=15,
        ),
        Reconciliation(),
        identity=Identity(),
    )
    assert "hour='4', minute='15'" in str(moved.get_job("identity-reconcile").trigger)
    for settings in (Settings(identity_reconcile_enabled=False), alone):
        off = build_scheduler(
            settings.model_copy(update={"identity_reconcile_enabled": False}),
            Reconciliation(),
            identity=Identity(),
        )
        assert off.get_job("identity-reconcile") is None
        assert all(callback.__name__ != "listener" for callback, _ in off._listeners)


async def test_runtime_schedules_bounded_derived_refresh(sessions):
    from app.services.runtime import build_runtime

    runtime = build_runtime(Settings(_env_file=None), sessions)
    try:
        scheduler = build_scheduler(Settings(), runtime.reconciliation, derived=runtime.derived)
        job = scheduler.get_job("derived-refresh")
        assert job.func == runtime.derived.run and job.max_instances == 1
        assert job.coalesce is True and str(job.trigger) == "interval[0:05:00]"
    finally:
        await runtime.shutdown()


def test_garmin_pull_runs_outside_the_nightly_window_only_when_enabled(tmp_path):
    from app.services.garmin_pull import GarminPull

    class Reconciliation:
        async def run(self, days):
            return days

    key = tmp_path / "key"
    key.write_text("synthetic")
    enabled = Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key)
    scheduler = build_scheduler(enabled, Reconciliation(), garmin=GarminPull(enabled, None))
    job = scheduler.get_job("garmin-pull")
    assert job.max_instances == 1 and job.coalesce is True
    trigger = str(job.trigger)
    assert "hour='0,4,6,8,10,12,14,16,18,20,22'" in trigger and "minute='10'" in trigger
    hourly = Settings(_env_file=None, garmin_enabled=True, garmin_pull_interval_minutes=60)
    moved = build_scheduler(hourly, Reconciliation(), garmin=GarminPull(hourly, None))
    assert "hour='0,1,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23'" in str(
        moved.get_job("garmin-pull").trigger
    )
    disabled = Settings(_env_file=None)
    off = build_scheduler(disabled, Reconciliation(), garmin=GarminPull(disabled, None))
    assert off.get_job("garmin-pull") is None
    assert build_scheduler(disabled, Reconciliation()).get_job("garmin-pull") is None
