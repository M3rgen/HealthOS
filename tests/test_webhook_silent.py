"""`webhook_silent`: no notification while the scheduled catch-up keeps finding data (synthetic).

The criterion is "no notification and new data found", so a quiet night is no silence. Only
value-free diagnostic events and delivery times are read.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.models.state import JobStatus, SyncState, WebhookEvent
from app.models.webhook_status import WebhookStatusEvent
from app.services.synchronization.pipeline_health import PipelineHealthService, PipelineStatus
from app.services.synchronization.webhook_status import WebhookStatusService, catch_up_silence

# Notifications stopped several hours before the panel is read.
NOW = datetime(2026, 11, 15, 18, 0, tzinfo=UTC)
LAST = -385
RECEIVER = {
    "google_health_webhook_endpoint_uri": "https://webhook.example.com/webhooks/google-health",
    "google_health_webhook_authorization": "Bearer " + "s" * 43,
}


def at(minute):
    return NOW + timedelta(minutes=minute)


async def seed(sessions, last, found):
    async with sessions.begin() as session:
        session.add(
            SyncState(data_type="steps", status=JobStatus.completed, last_successful_sync=at(-5))
        )
        if last is not None:
            session.add(WebhookEvent(payload_hash="a" * 64, raw_json={}, created_at=at(last)))
            session.add(WebhookStatusEvent(reason="notification_received", occurred_at=at(last)))
        session.add_all(
            WebhookStatusEvent(reason="catch_up_found", occurred_at=at(minute)) for minute in found
        )


@pytest.mark.parametrize(
    "last, found, receiver, silent, rounds",
    [
        # The catch-up kept finding records hours after the last notification.
        (LAST, [-370, -300, -240, -60], True, True, 4),
        # A quiet night: no notification and nothing new either.
        (LAST, [], True, False, 0),
        # Found only right after the last notification (a record Google did not name), then quiet.
        (LAST, [-375], True, False, 1),
        # Found after the threshold, but a notification may still follow within minutes.
        (LAST, [-10], True, False, 1),
        # Notifications arrive: what the catch-up finds is no silence.
        (-60, [-30], True, False, 1),
        # No receiver is configured: nothing to be silent.
        (LAST, [-60], False, False, 1),
        # Nothing was ever received: the seven-day silence covers it, not this reason.
        (None, [-60], True, False, 1),
    ],
)
async def test_silence_needs_no_notification_and_new_data_found(
    sessions, last, found, receiver, silent, rounds
):
    settings = Settings(_env_file=None, **(RECEIVER if receiver else {}))
    await seed(sessions, last, found)

    async with sessions() as session:
        report = await catch_up_silence(session, settings, at(last) if last else None, NOW)
    assert (report["silent"], report["catch_up_rounds_with_new_records"]) == (silent, rounds)
    assert report["last_found_at"] == (at(max(found)) if found else None)
    assert report["threshold_minutes"] == 120

    health = await PipelineHealthService(settings, sessions, {"steps"}).inspect(now=NOW)
    assert ("webhook_silent" in health.reasons) == silent
    assert health.status == (PipelineStatus.degraded if silent else PipelineStatus.healthy)

    status = await WebhookStatusService(settings, sessions, clock=lambda: NOW).snapshot()
    assert (status["silence"]["state"] == "missed") == silent
    assert status["silence"]["warning"] == silent
    assert status["counters"]["reasons"]["catch_up_found"] == len(found)


async def test_the_threshold_is_a_setting(sessions):
    await seed(sessions, LAST, [-60])
    settings = Settings(_env_file=None, webhook_silent_after_minutes=400, **RECEIVER)
    async with sessions() as session:
        report = await catch_up_silence(session, settings, at(LAST), NOW)
    assert (report["silent"], report["threshold_minutes"]) == (False, 400)
