"""Google data stays fresh without webhooks: the scheduled catch-up (synthetic data, fake clock).

On 15 November 2026 Google stopped notifying for hours while it held new records. The timer
fetches each data type's incremental window, costs no request while notifications keep it
covered, skips a busy type without waiting and never turns a failure into a storm.
"""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.oauth_keys import GoogleReauthorizationRequired
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.google_health.errors import GoogleHealthError
from app.models.state import RawHealthData, SyncState, WebhookNotification
from app.services.providers.ingestion import GoogleSource
from app.services.synchronization import retrieval
from app.services.synchronization.scheduler import build_scheduler
from app.sync import incremental as incremental_module
from app.sync import webhook_processing
from app.sync.incremental import IncrementalSync
from app.sync.ownership import run_ownership
from app.sync.reconciliation import ReconciliationService
from app.sync.scheduled_catch_up import ScheduledCatchUp
from app.sync.webhook_processing import WebhookBatchProcessor

NOW = datetime(2026, 11, 15, 11, 6, tzinfo=UTC)
SPECS = {"heart-rate": REGISTRY["heart-rate"], "sleep": REGISTRY["sleep"]}


def heart_rate(name, at):
    return {
        "name": f"users/1/dataTypes/heart-rate/dataPoints/{name}",
        "heartRate": {"sampleTime": {"physicalTime": at, "utcOffset": "0s"}, "beatsPerMinute": 61},
    }


def sleep(name, start, end):
    return {
        "name": f"users/1/dataTypes/sleep/dataPoints/{name}",
        "sleep": {"interval": {"startTime": start, "endTime": end}},
    }


def filter_time(record):
    """What each type's filter compares: the sample time, or a sleep session's end."""
    if "heartRate" in record:
        return record["heartRate"]["sampleTime"]["physicalTime"]
    return record["sleep"]["interval"]["endTime"]


class Google:
    """Answers each filter from what was uploaded so far and counts every request."""

    def __init__(self):
        self.uploaded = []
        self.requests = []
        self.warm_ups = []
        self.clock = None
        self.credentials = None  # an exception: the Google connection needs a new consent
        self.failures = {}  # data type -> exception raised by its list request

    def minute(self):
        return int((self.clock.now - NOW).total_seconds() // 60)

    async def warm_up(self):
        self.warm_ups.append(self.minute())
        if self.credentials is not None:
            raise self.credentials

    async def list_data_points(self, spec, start, end, page_token=None):
        self.requests.append((spec.name, self.minute(), start, end))
        if spec.name in self.failures:
            raise self.failures[spec.name]
        records = [
            record
            for record in self.uploaded
            if record["name"].split("/")[3] == spec.name
            and start <= datetime.fromisoformat(filter_time(record)) < end
        ]
        return Page(records, None)

    def fetched(self, data_type):
        return [minute for name, minute, *_ in self.requests if name == data_type]


class Diagnostics:
    def __init__(self):
        self.reasons = []

    async def record(self, *reasons):
        self.reasons.extend(reasons)


@pytest.fixture
def frozen(clock, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - signature of datetime.now
            return clock.now

    for module in (incremental_module, retrieval, webhook_processing):
        monkeypatch.setattr(module, "datetime", Frozen)
    clock.now = NOW
    return clock


@pytest.fixture
def google(frozen):
    client = Google()
    client.clock = frozen
    return client


@pytest.fixture
def sync(sessions, google):
    return IncrementalSync(Settings(_env_file=None), sessions, GoogleSource(google), dict(SPECS))


@pytest.fixture
def timer(sync, frozen):
    return ScheduledCatchUp(Settings(_env_file=None), sync, Diagnostics(), clock=lambda: frozen.now)


async def full_fetch(sync, frozen, *, at=NOW, days=7):
    frozen.now = at
    for name in sync.specs:
        await sync.run(name, at - timedelta(days=days), at)


async def tick(timer, frozen, minutes):
    """One scheduler tick per entry; the outcome of every type looked at."""
    outcomes = []
    for minute in minutes:
        frozen.now = NOW + timedelta(minutes=minute)
        outcomes.append(await timer.run())
    return outcomes


async def stored(sessions):
    async with sessions() as session:
        names = await session.scalars(select(RawHealthData.google_data_point_id))
    return {name.rsplit("/", 1)[1] for name in names}


async def test_new_records_arrive_on_the_timer_without_any_notification(
    sessions, google, sync, timer, frozen
):
    await full_fetch(sync, frozen)
    # Uploaded after the last full fetch; Google sends no notification.
    google.uploaded += [
        heart_rate("pulse", "2026-11-15T11:20:00Z"),
        sleep("nap", "2026-11-15T11:10:00Z", "2026-11-15T11:40:00Z"),
    ]
    before = len(google.requests)

    await tick(timer, frozen, range(5, 185, 5))

    assert {"pulse", "nap"} <= await stored(sessions)
    # Both types checked every 15 minutes, staggered; fetched once nothing covered them for
    # 45 minutes. Two types: four plus three requests in three hours.
    assert google.fetched("heart-rate")[-3:] == [50, 95, 140]
    assert google.fetched("sleep")[-4:] == [45, 90, 135, 180]
    assert len(google.requests) - before == 7
    # Each window runs from the last success minus the type's safety window, up to now.
    name, minute, start, end = google.requests[-1]
    assert (name, end - start) == ("sleep", timedelta(minutes=45 + 72 * 60))
    assert timer.status.reasons == ["catch_up_found", "catch_up_found"]


async def notify(sessions, sync, element_hash):
    async with sessions.begin() as session:
        session.add(
            WebhookNotification(
                element_hash=element_hash,
                raw_json={},
                is_valid=True,
                data_type="sleep",
                health_user_id="user-1",
                operation="UPSERT",
                intervals=[
                    {
                        "physicalTimeInterval": {
                            "startTime": "2026-11-15T01:00:00Z",
                            "endTime": "2026-11-15T07:00:00Z",
                        }
                    }
                ],
            )
        )
    return await WebhookBatchProcessor(sessions, sync, set(sync.specs))._process_element(
        element_hash
    )


@pytest.mark.parametrize("with_timer", [False, True])
async def test_regular_notifications_leave_the_timer_without_requests(
    sessions, google, frozen, with_timer
):
    sync = IncrementalSync(
        Settings(_env_file=None), sessions, GoogleSource(google), {"sleep": SPECS["sleep"]}
    )
    timer = ScheduledCatchUp(Settings(_env_file=None), sync, clock=lambda: frozen.now)
    await full_fetch(sync, frozen)
    outcomes = []
    # Six hours of sleep notifications every 14 minutes, as before the incident.
    for minute in range(1, 361):
        frozen.now = NOW + timedelta(minutes=minute)
        if with_timer and minute % 5 == 0:
            outcomes.append(await timer.run())
        if minute % 14 == 10:
            assert await notify(sessions, sync, f"n{minute}") == "completed"

    assert "fetched" not in {value for outcome in outcomes for value in outcome.values()}
    assert len(outcomes) == (72 if with_timer else 0)
    # The full fetch, 26 notified ranges and the notifications' own catch-ups (every 42
    # minutes), the same with and without the timer.
    assert len(google.requests) == 1 + 26 + 8


async def test_a_busy_type_is_skipped_without_waiting_and_fetched_next_tick(
    sessions, google, sync, timer, frozen
):
    await full_fetch(sync, frozen, at=NOW - timedelta(hours=2))
    google.uploaded.append(heart_rate("pulse", "2026-11-15T11:00:00Z"))
    frozen.now = NOW
    async with run_ownership(sessions, "heart-rate") as held:
        assert held is not None
        # A reconciliation window or a notification holds the type: no queue, no request.
        assert await tick(timer, frozen, [5]) == [{"heart-rate": "busy"}]
        assert google.fetched("heart-rate") == [-120]

    assert await tick(timer, frozen, [10]) == [{"heart-rate": "fetched"}]
    assert google.fetched("heart-rate") == [-120, 10]
    assert "pulse" in await stored(sessions)


@pytest.mark.parametrize(
    "failure, requests",
    [
        # The Google connection needs a new consent: checked before any fetch, so no request
        # and no failed sync state.
        ("credentials", 0),
        # Google answers 503 after its retries: one request per attempt, never more.
        ("google", 6),
    ],
)
async def test_a_failure_pauses_every_type_with_doubling_delays(
    sessions, google, sync, timer, frozen, caplog, monkeypatch, failure, requests
):
    # Alembic's fileConfig in the migration tests disables already-created loggers.
    monkeypatch.setattr(logging.getLogger("app.sync.scheduled_catch_up"), "disabled", False)
    await full_fetch(sync, frozen, at=NOW - timedelta(hours=2))
    google.uploaded.append(heart_rate("pulse", "2026-11-15T11:00:00Z"))
    warm_ups = len(google.warm_ups)
    if failure == "credentials":
        google.credentials = GoogleReauthorizationRequired()
    else:
        google.failures = dict.fromkeys(SPECS, GoogleHealthError("synthetic", http_status=503))

    await tick(timer, frozen, range(5, 185, 5))

    # Attempts after pauses of 5, 10, 20, 40 and 60 minutes; the other type waits as well.
    assert google.warm_ups[warm_ups:] == [5, 10, 20, 40, 80, 140]
    assert len(google.requests) - 2 == requests
    paused = [record for record in caplog.records if record.message.startswith("Scheduled")]
    assert [(record.levelno, record.event) for record in paused] == [
        (logging.WARNING, "SCHEDULED_CATCH_UP_PAUSED")
    ]
    async with sessions() as session:
        states = {row.data_type: row.status for row in await session.scalars(select(SyncState))}
    assert states == {"heart-rate": "failed" if requests else "completed", "sleep": "completed"}

    google.credentials, google.failures = None, {}
    outcomes = await tick(timer, frozen, [200])
    assert outcomes == [{"heart-rate": "fetched", "sleep": "fetched"}]
    assert "pulse" in await stored(sessions)


async def test_a_failing_type_backs_off_alone(sessions, google, sync, timer, frozen):
    await full_fetch(sync, frozen, at=NOW - timedelta(hours=2))
    google.failures = {"sleep": GoogleHealthError("synthetic", http_status=403)}

    await tick(timer, frozen, range(5, 245, 5))

    # Twice, four and eight times its 15 minutes after each failure; heart rate goes on.
    assert google.fetched("sleep")[1:] == [15, 45, 105, 225]
    assert google.fetched("heart-rate")[1:] == [5, 50, 95, 140, 185, 230]


async def test_a_reconciliation_lets_a_due_catch_up_run_between_its_windows(
    sessions, google, frozen
):
    settings = Settings(_env_file=None)
    sync = IncrementalSync(
        settings, sessions, GoogleSource(google), {"heart-rate": SPECS["heart-rate"]}
    )
    timer = ScheduledCatchUp(settings, sync, clock=lambda: frozen.now)
    await full_fetch(sync, frozen, at=NOW - timedelta(hours=3))
    google.uploaded.append(heart_rate("pulse", "2026-11-15T11:00:00Z"))
    frozen.now = NOW
    reconciliation = ReconciliationService(sync, sessions, slice_days=7, catch_up=timer)
    before = len(google.requests)

    await reconciliation.run(20)

    windows = [(end - start).days for _, _, start, end in google.requests[before:]]
    # 7-day windows, oldest first; the recent hour is fetched after the first window, then
    # covered for the rest of the pass.
    assert windows == [7, 0, 7, 6]
    assert google.requests[before + 1][3] == NOW
    assert "pulse" in await stored(sessions)


@pytest.mark.parametrize(
    "enabled, catch_up, jobs",
    [
        (True, SimpleNamespace(run=AsyncMock(), tick=timedelta(minutes=5)), 1),
        (False, SimpleNamespace(run=AsyncMock(), tick=timedelta(minutes=5)), 0),
        # A runner without a catch-up (an older caller, a test double) schedules none.
        (True, None, 0),
    ],
)
def test_the_catch_up_job_follows_the_setting(enabled, catch_up, jobs):
    reconciliation = SimpleNamespace(run=AsyncMock(), catch_up=catch_up)
    scheduler = build_scheduler(Settings(_env_file=None, catch_up_enabled=enabled), reconciliation)
    job = scheduler.get_job("scheduled-catch-up")
    assert (job is not None) == bool(jobs)
    if job is not None:
        assert "0:05:00" in str(job.trigger)
        assert (job.max_instances, job.coalesce) == (1, True)
