"""Scanning a window for source deletions in pages: every row once, none skipped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.google_health.data_types import REGISTRY
from app.services.archive import source_deletions as deletions
from app.services.archive.storage import store_records
from app.services.identities.identity_presence import Presence

SPEC = REGISTRY["steps"]
START = datetime(2026, 8, 1, tzinfo=UTC)


def steps(hour: int) -> dict:
    return {
        "name": f"users/1/dataTypes/steps/dataPoints/p{hour}",
        "steps": {
            "interval": {
                "startTime": f"2026-08-01T{hour:02d}:00:00Z",
                "endTime": f"2026-08-01T{hour:02d}:01:00Z",
            },
            "count": "10",
        },
    }


async def test_each_row_is_decided_once_across_page_boundaries(sessions, monkeypatch):
    monkeypatch.setattr(deletions, "CHUNK", 2)
    async with sessions.begin() as session:
        await store_records(session, SPEC, [steps(hour) for hour in range(5)])
    visited: list = []

    async def everything_present(session, spec, chunk, seen_since, chains=None):
        visited.extend(raw_id for raw_id, _, _ in chunk)
        return Presence(present={raw_id for raw_id, _, _ in chunk})

    monkeypatch.setattr(deletions, "present_records", everything_present)
    async with sessions.begin() as session:
        await deletions.mark_source_deletions(
            session, SPEC, START, START + timedelta(days=1), datetime(9999, 1, 1, tzinfo=UTC)
        )
    assert len(visited) == 5 and len(set(visited)) == 5
