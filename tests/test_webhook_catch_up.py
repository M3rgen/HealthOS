"""A webhook that names a narrow or stale range still brings late uploads (synthetic data).

Google may keep notifying one record's interval while another record of the same type,
uploaded after the last full fetch, lies outside it. The notified range is fetched as
before; the data type's incremental window is caught up too, at most once per
CATCH_UP_INTERVAL.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import RawHealthData, SyncState, WebhookNotification
from app.services.providers.ingestion import GoogleSource
from app.services.synchronization import retrieval
from app.sync import incremental as incremental_module
from app.sync import webhook_processing
from app.sync.incremental import IncrementalSync
from app.sync.webhook_processing import WebhookBatchProcessor

NOW = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
MORNING = ("2026-11-01T05:00:00Z", "2026-11-01T11:00:00Z")
EVENING = ("2026-11-01T19:00:00Z", "2026-11-01T23:30:00Z")


def sleep(name, start, end):
    return {
        "name": f"users/1/dataTypes/sleep/dataPoints/{name}",
        "sleep": {"interval": {"startTime": start, "endTime": end}},
    }


class Google:
    """Answers the sleep filter (end time in [start, end)) from what was uploaded so far."""

    def __init__(self, *uploaded):
        self.uploaded = list(uploaded)
        self.windows = []

    async def warm_up(self):
        pass

    async def list_data_points(self, spec, start, end, page_token=None):
        self.windows.append((start, end))
        return Page(
            [
                record
                for record in self.uploaded
                if start <= datetime.fromisoformat(record["sleep"]["interval"]["endTime"]) < end
            ],
            None,
        )


@pytest.fixture
def frozen(clock, monkeypatch):
    """One wall clock for the sync, its receipts, webhook processing and storage."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 - signature of datetime.now
            return clock.now

    for module in (incremental_module, retrieval, webhook_processing):
        monkeypatch.setattr(module, "datetime", Frozen)
    clock.now = NOW
    return clock


@pytest.fixture
def google():
    return Google(sleep("morning", *MORNING))


@pytest.fixture
def sync(sessions, google):
    return IncrementalSync(
        Settings(_env_file=None), sessions, GoogleSource(google), {"sleep": REGISTRY["sleep"]}
    )


async def notify(sessions, sync, element_hash, interval=MORNING):
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
                    {"physicalTimeInterval": {"startTime": interval[0], "endTime": interval[1]}}
                ],
            )
        )
    processor = WebhookBatchProcessor(sessions, sync, set(sync.specs))
    return await processor._process_element(element_hash)


async def stored(sessions):
    async with sessions() as session:
        names = await session.scalars(select(RawHealthData.google_data_point_id))
    return {name.rsplit("/", 1)[1] for name in names}


async def test_narrow_notification_also_brings_a_record_uploaded_outside_it(
    sessions, frozen, google, sync
):
    # The last full fetch ran before the evening session reached Google.
    await sync.run("sleep", NOW - timedelta(days=2), NOW)
    google.uploaded.append(sleep("evening", *EVENING))

    frozen.now = NOW + timedelta(hours=1)
    assert await notify(sessions, sync, "morning-again") == "completed"

    assert await stored(sessions) == {"morning", "evening"}


# Webhooks every 14 minutes after a full fetch at NOW.
EVERY_14 = [10 + 14 * step for step in range(26)]


@pytest.mark.parametrize(
    "days, notified, catch_ups",
    [
        # The incident's 2-day recovery run does not reach back over sleep's 72-hour safety
        # window: the first notification catches up, then at most once per 30 minutes.
        (2, EVERY_14[:5], [10, 52]),
        # The daily 7-day reconciliation counts as a catch-up for its first 30 minutes.
        (7, EVERY_14[:5], [38]),
        # Six hours of notifications: 26 requests before, 26 + 9 now.
        (2, EVERY_14, [10, 52, 94, 136, 178, 220, 262, 304, 346]),
    ],
)
async def test_catch_up_runs_at_most_once_per_interval(
    sessions, frozen, google, sync, days, notified, catch_ups
):
    await sync.run("sleep", NOW - timedelta(days=days), NOW)
    google.uploaded.append(sleep("evening", *EVENING))
    fetched = []
    for minute in notified:
        frozen.now = NOW + timedelta(minutes=minute)
        before = len(google.windows)
        assert await notify(sessions, sync, f"n{minute}") == "completed"
        # The notified range is fetched every time, and first.
        assert google.windows[before][1] > datetime.fromisoformat(MORNING[1])
        if len(google.windows) - before == 2:
            fetched.append(minute)
    assert fetched == catch_ups
    assert len(google.windows) == 1 + len(notified) + len(catch_ups)
    assert await stored(sessions) == {"morning", "evening"}


class FailingGoogle(Google):
    def __init__(self, *uploaded, fail_calls=()):
        super().__init__(*uploaded)
        self.fail_calls = set(fail_calls)

    async def list_data_points(self, spec, start, end, page_token=None):
        if len(self.windows) in self.fail_calls:
            self.windows.append((start, end))
            raise RuntimeError("synthetic Google failure")
        return await super().list_data_points(spec, start, end, page_token)


@pytest.mark.parametrize(
    "fail_call, status, requests, logged",
    [
        # The catch-up request fails: the notification keeps its completed result.
        (2, "completed", 3, "WEBHOOK_CATCH_UP_FAILED"),
        # Its own range fails: it stays failed for the replay, and nothing is caught up.
        (1, "failed", 2, "WEBHOOK_ELEMENT_FAILED"),
    ],
)
async def test_catch_up_never_changes_the_notification_outcome(
    sessions, frozen, caplog, fail_call, status, requests, logged
):
    google = FailingGoogle(sleep("morning", *MORNING), fail_calls={fail_call})
    sync = IncrementalSync(
        Settings(_env_file=None), sessions, GoogleSource(google), {"sleep": REGISTRY["sleep"]}
    )
    await sync.run("sleep", NOW - timedelta(days=2), NOW)
    frozen.now = NOW + timedelta(hours=1)

    assert await notify(sessions, sync, "n1") == status

    assert len(google.windows) == requests
    assert logged in {getattr(record, "event", None) for record in caplog.records}
    async with sessions() as session:
        row = await session.get(WebhookNotification, "n1")
    assert (row.sync_attempt_count, row.sync_success_count) == (1, int(status == "completed"))


async def test_concurrent_notifications_of_one_type_catch_up_once(sessions, frozen, google, sync):
    await sync.run("sleep", NOW - timedelta(days=2), NOW)
    google.uploaded.append(sleep("evening", *EVENING))
    frozen.now = NOW + timedelta(hours=1)

    results = await asyncio.gather(*(notify(sessions, sync, name) for name in ("a", "b")))

    assert results == ["completed", "completed"]
    # One full fetch, two notified ranges and a single catch-up decided under the type lock.
    assert len(google.windows) == 1 + 2 + 1
    assert await stored(sessions) == {"morning", "evening"}


@pytest.mark.parametrize(
    "data_type, last_success, expected",
    [
        # The incremental window: the last success minus the type's safety window.
        ("sleep", NOW - timedelta(hours=1), NOW - timedelta(hours=73)),
        ("heart-rate", NOW - timedelta(hours=1), NOW - timedelta(hours=2)),
        # A rollup type never asks for more than its 14-day limit.
        ("total-calories", NOW - timedelta(days=30), NOW - timedelta(days=14)),
    ],
)
async def test_catch_up_window_follows_the_incremental_start(
    sessions, data_type, last_success, expected
):
    sync = IncrementalSync(Settings(_env_file=None), sessions, GoogleSource(Google()), REGISTRY)
    async with sessions.begin() as session:
        session.add(SyncState(data_type=data_type, last_successful_sync=last_success))

    assert await sync._catch_up_start(sessions, data_type, NOW) == expected
