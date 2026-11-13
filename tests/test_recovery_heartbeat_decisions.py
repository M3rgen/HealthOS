"""Recovery's age threshold and its second heartbeat check protect live work."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, SyncState
from app.services.providers.ingestion import GoogleSource
from app.sync import incremental

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def service(sessions):
    return incremental.IncrementalSync(
        Settings(_env_file=None), sessions, GoogleSource(None), {"steps": REGISTRY["steps"]}
    )


@pytest.mark.parametrize("age,recovered", [(2, 0), (14, 0), (15, 0), (16, 1)])
async def test_recovery_observes_fifteen_minute_grace(sessions, age, recovered):
    heartbeat = NOW - timedelta(minutes=age)
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="steps",
                status=JobStatus.running,
                updated_at=heartbeat,
                last_page_token="resume",
                last_requested_from=NOW - timedelta(days=1),
                last_requested_to=NOW,
            )
        )
    assert await service(sessions).recover_interrupted(now=NOW) == recovered
    async with sessions() as session:
        state = await session.get(SyncState, "steps")
        assert state.status == (JobStatus.failed if recovered else JobStatus.running)
        assert state.last_page_token == "resume"
        assert as_utc(state.last_requested_to) == NOW
        assert state.last_error == (incremental.INTERRUPTED_ERROR if recovered else None)


async def test_heartbeat_refreshed_between_selection_and_ownership_is_not_failed(
    sessions, monkeypatch
):
    async with sessions.begin() as session:
        session.add(
            SyncState(
                data_type="steps",
                status=JobStatus.running,
                updated_at=NOW - timedelta(minutes=20),
                last_page_token="resume",
            )
        )
    actual = incremental.run_ownership
    selected = []

    @asynccontextmanager
    async def refresh_before_ownership(factory, data_type, *, wait):
        selected.append(data_type)
        async with sessions.begin() as session:
            state = await session.get(SyncState, data_type)
            state.updated_at = NOW  # the worker published a heartbeat after candidate selection
        async with actual(factory, data_type, wait=wait) as owned:
            yield owned

    monkeypatch.setattr(incremental, "run_ownership", refresh_before_ownership)
    assert await service(sessions).recover_interrupted(now=NOW) == 0
    assert selected == ["steps"]
    async with sessions() as session:
        state = await session.get(SyncState, "steps")
        assert state.status == JobStatus.running and state.last_error is None
        assert as_utc(state.updated_at) == NOW and state.last_page_token == "resume"
