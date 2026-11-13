"""The nightly identity reconciler and a busy data type (issue #262), on SQLite.

The daily Google reconciliation held heart-rate's shared lock for up to an hour; the reconciler,
started at a fixed 02:30 UTC, gave up after its eight attempts night after night (and a run by
hand during the watchdog's recovery after a restart did the same). Now it follows the scheduled
reconciliations, tries a still-busy data type again after the others until the window ends (or
for IDENTITY_RECONCILE_LOCK_WAIT_MINUTES outside it), and status and /health say how many nights
a data type went unsettled. The lock is
replaced by a fake that is held until a given time of a fake clock that each back-off advances.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

from apscheduler.events import (
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobExecutionEvent,
    JobSubmissionEvent,
)
from identity_records import HEART, NEW_NAME, STEPS, T0, heart, legacy, steps, store

from app.core.config import Settings
from app.models.identity_tables import IdentityReconcileState
from app.services.identities import identity_backfill as backfill
from app.services.identities import identity_reconcile_window as window
from app.services.identities.identity_backfill import identity_backfill
from app.services.identities.identity_options import BackfillOptions
from app.services.identities.identity_reconciler import IdentityReconciler
from app.services.identities.source_chain_locks import LockBusy
from app.services.synchronization.pipeline_health import PipelineHealthService
from app.services.synchronization.scheduler import build_scheduler

NIGHT = datetime(2026, 10, 19, 2, 30, tzinfo=UTC)
# Explicit, never read from the environment: CI exports RECONCILIATION_ENABLED=false, which a
# bare Settings() picks up (the identity job then keeps its fixed time instead of the chain).
CHAINED = Settings(_env_file=None, reconciliation_enabled=True)
UNTIL = time(3, 55)


class HeldLock:
    """heart-rate's lock, held by a long reconciliation until ``released`` (``None``: never)."""

    def __init__(self, monkeypatch, released: datetime | None) -> None:
        self.now = NIGHT
        self.released = released
        self.busy = 0
        self.granted: list[str] = []
        monkeypatch.setattr(window, "_now", lambda: self.now)
        monkeypatch.setattr(backfill, "_pause", self.pause)
        monkeypatch.setattr(backfill, "lock_data_type", self.lock)

    async def pause(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def lock(self, session, data_type, *, exclusive, wait):
        assert exclusive and wait == backfill.LOCK_TIMEOUT
        if data_type == HEART.name and (self.released is None or self.now < self.released):
            self.busy += 1
            raise LockBusy("held by the daily reconciliation")
        self.granted.append(data_type)


async def archive(sessions, clock) -> None:
    """A full run, then a renamed heart-rate source and new steps for the nightly run."""
    await legacy(sessions, HEART, heart(60), T0, last=T0 + timedelta(hours=1))
    await legacy(sessions, STEPS, steps(100), T0)
    await identity_backfill(sessions)
    clock.now = T0 + timedelta(hours=9)
    await store(sessions, HEART, heart(80, at=T0 + timedelta(hours=9), name=NEW_NAME))
    await store(sessions, STEPS, steps(120, start=T0 + timedelta(hours=9)))


async def state_of(sessions, data_type) -> IdentityReconcileState:
    async with sessions() as session:
        found = await session.get(IdentityReconcileState, data_type)
        assert found is not None
        return found


async def test_a_held_lock_is_waited_for_and_the_data_type_settled_the_same_night(
    sessions, clock, monkeypatch
):
    await archive(sessions, clock)
    lock = HeldLock(monkeypatch, released=NIGHT + timedelta(minutes=40))

    summary = await IdentityReconciler(sessions, UNTIL).run()

    assert summary is not None
    assert summary["heart-rate"]["error"] is None
    assert summary["heart-rate"]["review_suggested"] == 1  # the rename was decided tonight
    # It did not give up after LOCK_ATTEMPTS; steps did not wait behind heart-rate.
    assert lock.busy > backfill.LOCK_ATTEMPTS
    assert lock.granted.index(STEPS.name) < lock.granted.index(HEART.name)
    assert NIGHT + timedelta(minutes=40) <= lock.now < NIGHT + timedelta(minutes=42)
    state = await state_of(sessions, HEART.name)
    assert state.last_error is None and window.failed_nights(state) is None


async def test_the_window_end_leaves_the_data_type_failed_and_counted(sessions, clock, monkeypatch):
    await archive(sessions, clock)
    lock = HeldLock(monkeypatch, released=None)
    notices: list[str] = []

    results = await identity_backfill(
        sessions,
        incremental=True,
        lock_deadline=window.busy_deadline(UNTIL, 60),
        options=BackfillOptions(notice=notices.append),
    )

    error = results["heart-rate"].error or ""
    assert window.LOCK_ERROR in error and "until 03:55 UTC" in error
    assert results["steps"].error is None
    # Backed off up to 60 s at a time, to the window end and no further.
    assert datetime(2026, 10, 19, 3, 55, tzinfo=UTC) <= lock.now
    assert lock.now < datetime(2026, 10, 19, 3, 57, tzinfo=UTC)
    assert any("(attempt 9 until 03:55 UTC)" in notice for notice in notices)
    state = await state_of(sessions, HEART.name)
    assert window.lock_failed(state.last_error) and window.failed_nights(state) == 1
    service = PipelineHealthService(Settings(), sessions, {HEART.name})
    snapshot = await service.inspect(now=lock.now)
    [contention] = snapshot.as_dict()["identity_lock_contention"]
    assert contention["data_type"] == "heart-rate" and contention["nights"] == 1
    # A warning: it does not change the pipeline status on its own.
    assert "identity_lock_contention" not in snapshot.reasons


async def test_after_the_window_a_recovery_sync_is_waited_for_the_configured_minutes(
    sessions, clock, monkeypatch
):
    """The daytime case: the watchdog's recovery after a restart (or a sync by hand) holds
    heart-rate; a reconciler run then waits IDENTITY_RECONCILE_LOCK_WAIT_MINUTES, not 8 tries."""
    await archive(sessions, clock)
    morning = datetime(2026, 10, 19, 10, 40, tzinfo=UTC)
    lock = HeldLock(monkeypatch, released=morning + timedelta(minutes=25))
    lock.now = morning

    summary = await IdentityReconciler(sessions, UNTIL, wait_minutes=60).run()

    assert summary is not None and summary["heart-rate"]["error"] is None
    assert lock.busy > backfill.LOCK_ATTEMPTS
    assert morning + timedelta(minutes=25) <= lock.now < morning + timedelta(minutes=27)


async def test_without_a_wait_after_the_window_eight_attempts_remain(sessions, clock, monkeypatch):
    await archive(sessions, clock)
    lock = HeldLock(monkeypatch, released=None)
    lock.now = datetime(2026, 10, 19, 7, 30, tzinfo=UTC)

    summary = await IdentityReconciler(sessions, UNTIL, wait_minutes=0).run()

    assert summary is not None and "in 8 attempts (" in summary["heart-rate"]["error"]
    assert lock.busy == backfill.LOCK_ATTEMPTS
    assert summary["steps"]["error"] is None


def test_failed_nights_count_the_nights_since_the_checkpoint():
    settled = datetime(2026, 11, 4, 2, 30, tzinfo=UTC)
    failing = SimpleNamespace(
        data_type="heart-rate",
        last_error="heart-rate: could not get the identity lock (or a row it needs) in 8 attempts",
        checkpoint_at=settled,
        last_started_at=datetime(2026, 11, 7, 2, 30, tzinfo=UTC),
    )
    other = SimpleNamespace(
        data_type="steps",
        last_error="canceling statement due to statement timeout",
        checkpoint_at=settled,
        last_started_at=datetime(2026, 11, 5, 2, 30, tzinfo=UTC),
    )
    fine = SimpleNamespace(
        data_type="sleep", last_error=None, checkpoint_at=settled, last_started_at=settled
    )
    assert [window.failed_nights(state) for state in (failing, other, fine)] == [3, 1, None]
    assert window.lock_contention([other, fine, failing]) == [
        {
            "data_type": "heart-rate",
            "nights": 3,
            "since": "2026-11-04T02:30:00Z",
            "last_attempt": "2026-11-07T02:30:00Z",
        }
    ]
    assert window.busy_deadline(UNTIL, 60, NIGHT) == datetime(2026, 10, 19, 3, 55, tzinfo=UTC)
    late = NIGHT + timedelta(hours=2)
    assert window.busy_deadline(UNTIL, 60, late) == late + timedelta(minutes=60)
    assert window.busy_deadline(None, 15, NIGHT) == NIGHT + timedelta(minutes=15)
    assert window.busy_deadline(UNTIL, 0, late) is None


class Reconciliation:
    def __init__(self) -> None:
        self.ran: list[int] = []

    async def run(self, days):
        await asyncio.sleep(0)
        self.ran.append(days)
        return {}


class Identity:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self):
        self.started.set()


async def test_the_reconciler_starts_when_the_daily_reconciliation_ends():
    reconciliation, identity = Reconciliation(), Identity()
    scheduler = build_scheduler(CHAINED, reconciliation, identity=identity)
    scheduler.start()
    try:
        assert scheduler.get_job("identity-reconcile") is None
        scheduler.get_job("daily-reconciliation").modify(next_run_time=datetime.now(UTC))
        scheduler.wakeup()
        await asyncio.wait_for(identity.started.wait(), timeout=30)
        assert reconciliation.ran == [7]
    finally:
        await scheduler.shutdown_and_wait()


def test_the_reconciler_waits_for_every_reconciliation_in_flight():
    scheduler = build_scheduler(CHAINED, Reconciliation(), identity=Identity())
    now = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)

    def dispatch(code: int, job_id: str) -> None:
        if code == EVENT_JOB_SUBMITTED:
            scheduler._dispatch_event(JobSubmissionEvent(code, job_id, "default", [now]))
        else:
            scheduler._dispatch_event(JobExecutionEvent(code, job_id, "default", now))

    # Sunday: the weekly run queued behind a daily run that overran 03:00.
    dispatch(EVENT_JOB_SUBMITTED, "daily-reconciliation")
    dispatch(EVENT_JOB_SUBMITTED, "weekly-reconciliation")
    dispatch(EVENT_JOB_EXECUTED, "daily-reconciliation")
    assert scheduler.get_job("identity-reconcile") is None
    dispatch(EVENT_JOB_EXECUTED, "weekly-reconciliation")
    job = scheduler.get_job("identity-reconcile")
    assert job is not None and type(job.trigger).__name__ == "DateTrigger"
    scheduler.remove_job("identity-reconcile")
    # A daily run missed altogether (the app was down) still starts it; other jobs do not.
    dispatch(EVENT_JOB_EXECUTED, "pipeline-watchdog")
    assert scheduler.get_job("identity-reconcile") is None
    dispatch(EVENT_JOB_MISSED, "daily-reconciliation")
    assert scheduler.get_job("identity-reconcile") is not None
