"""Backfills normalize previously archived activities without refetching."""

from __future__ import annotations

from datetime import timedelta

from garmin_records import daily_steps
from test_garmin_normalize import DAY, NOW, count, garmin_activity, raw

from app.core.config import Settings
from app.core.providers import GARMIN
from app.models.garmin import GarminPullState
from app.models.health import (
    ExerciseSession,
)
from app.services.providers.garmin_pull import KIND_NAMES


async def test_backfill_normalizes_activities_archived_before_normalization_existed(
    sessions, tmp_path, unlocked_pull
):
    """A phase-1 archive (activities first seen long before this run) gets its activities into
    ``exercise_sessions`` from a backfill over their window; the second run writes nothing."""
    key = tmp_path / "key"
    key.write_text("synthetic")
    old = NOW - timedelta(days=120)
    async with sessions.begin() as session:
        session.add_all(
            [
                raw("activities", "4001", garmin_activity(4001, DAY), seen=old, suffix="a"),
                raw(
                    "activities",
                    "4002",
                    garmin_activity(4002, DAY - timedelta(days=30)),
                    seen=old,
                    suffix="b",
                ),
                raw("steps", DAY.isoformat(), daily_steps(DAY), seen=old, suffix="c"),
                # Every kind already finished these bounds: the backfill fetches nothing.
                *(
                    GarminPullState(
                        kind=name,
                        backfill_since=DAY - timedelta(days=60),
                        backfill_until=DAY,
                        backfill_next=None,
                    )
                    for name in KIND_NAMES
                ),
            ]
        )
    pull = unlocked_pull(
        Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key),
        sessions,
        clock=lambda: NOW,
    )
    report = await pull.backfill(DAY - timedelta(days=60), DAY)
    assert all(entry["requests"] == 0 and entry["complete"] for entry in report["kinds"].values())
    assert report["normalized"]["tables"]["exercise_sessions"]["inserted"] == 2
    assert report["normalized"]["tables"]["steps"]["inserted"] == 1
    assert await count(sessions, ExerciseSession, provider=GARMIN) == 2
    again = await pull.backfill(DAY - timedelta(days=60), DAY)
    assert again["normalized"]["inserted"] == 0 and again["normalized"]["updated"] == 0
    # The periodic pull still reads only the activities it archived itself.
    stats = await pull.normalize(DAY, DAY, seen_since=NOW)
    assert stats.tables.get("exercise_sessions") is None and stats.unchanged >= 1
