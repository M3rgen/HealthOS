"""A page aborted by a deadlock is rolled back and stored exactly once by the retry."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.models.state import JobStatus, RawHealthData, SyncState
from app.services.providers import ingestion
from app.services.providers.ingestion import GoogleSource
from app.sync import errors
from app.sync.incremental import IncrementalSync

pytestmark = pytest.mark.integration

WINDOW_START = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 2, tzinfo=UTC)


class _Driver(Exception):
    sqlstate = "40P01"


class _Client:
    def __init__(self, pages: list[Page]) -> None:
        self.pages = pages

    async def list_data_points(self, spec, start, end, page_token=None):
        return self.pages.pop(0)

    async def keep_after(self, responses, failure) -> None:
        return None


def _record(name: str, minute: int) -> dict:
    return {
        "name": f"users/me/dataTypes/steps/dataPoints/{name}",
        "steps": {
            "interval": {
                "startTime": f"2026-08-01T10:{minute:02d}:00Z",
                "endTime": f"2026-08-01T10:{minute + 1:02d}:00Z",
            },
            "count": "10",
        },
    }


def _service(sessions, client) -> IncrementalSync:
    return IncrementalSync(
        Settings(_env_file=None, app_env="test"),
        sessions,
        GoogleSource(client),
        {"steps": REGISTRY["steps"]},
    )


def _stored_then_deadlocks(monkeypatch, failures: int) -> list[int]:
    """The real write runs first, so the retry has a real rollback to depend on."""
    real, calls = ingestion.store_page, []

    async def store(*args, **kwargs):
        stats = await real(*args, **kwargs)
        calls.append(1)
        if len(calls) <= failures:
            raise DBAPIError("synthetic", {}, _Driver())
        return stats

    monkeypatch.setattr(ingestion, "store_page", store)
    monkeypatch.setattr(errors, "CONFLICT_BACKOFF", 0)
    return calls


async def _state(sessions) -> tuple[int, SyncState]:
    async with sessions() as session:
        count = await session.scalar(select(func.count(RawHealthData.id)))
        state = await session.get(SyncState, "steps")
    assert count is not None and state is not None
    return count, state


async def test_deadlocked_page_is_retried_and_counted_once(pg_sessions, monkeypatch) -> None:
    calls = _stored_then_deadlocks(monkeypatch, failures=1)
    client = _Client([Page([_record("one", 0), _record("two", 2)], None)])

    stats = await _service(pg_sessions, client).run("steps", WINDOW_START, WINDOW_END)

    assert len(calls) == 2
    assert stats.inserted == 2
    count, state = await _state(pg_sessions)
    assert count == 2
    assert state.records_downloaded == 2  # the rolled-back attempt left no trace
    assert state.status == JobStatus.completed


async def test_exhausted_retries_fail_the_sync_as_a_named_conflict(
    pg_sessions, monkeypatch
) -> None:
    calls = _stored_then_deadlocks(monkeypatch, failures=errors.CONFLICT_ATTEMPTS)
    client = _Client([Page([_record("one", 0)], None)])

    with pytest.raises(DBAPIError):
        await _service(pg_sessions, client).run("steps", WINDOW_START, WINDOW_END)

    assert len(calls) == errors.CONFLICT_ATTEMPTS
    count, state = await _state(pg_sessions)
    assert count == 0  # every attempt rolled back
    assert state.status == JobStatus.failed
    assert state.last_error == "DBAPIError (database_deadlock)"
