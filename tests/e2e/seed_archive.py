"""Synthetic archive for the end-to-end journey: migrations, then months of invented readings.

Run by tests/e2e/run_journey.py in a throwaway database. Everything here is made up, and it goes
through the same storage functions as a sync, so the journey reads what a real archive holds.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "users/me/dataTypes"
HISTORY_DAYS = 45
MEASUREMENTS = 40
WORKOUTS = 12
# The latest complete day carries a step count the journey looks for on the page.
MARKER_STEPS = 9173


def migrate() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")


def stamp(day: date, hour: int, minute: int = 0) -> str:
    return datetime.combine(day, time(hour, minute), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def steps(day: date, count: int) -> dict:
    return {
        "name": f"{PREFIX}/steps/dataPoints/e2e-{day}",
        "steps": {
            "interval": {"startTime": stamp(day, 11), "endTime": stamp(day, 12)},
            "count": str(count),
        },
    }


def sleep(day: date) -> dict:
    return {
        "name": f"{PREFIX}/sleep/dataPoints/e2e-{day}",
        "sleep": {"interval": {"startTime": stamp(day, 0), "endTime": stamp(day, 7, 30)}},
    }


def resting(day: date, bpm: int) -> dict:
    return {
        "name": f"{PREFIX}/daily-resting-heart-rate/dataPoints/e2e-{day}",
        "dailyRestingHeartRate": {"date": day.isoformat(), "beatsPerMinute": bpm},
    }


async def seed() -> dict[str, str | int]:
    from app.core.config import get_settings
    from app.db.session import SessionLocal, initialize_sessions
    from app.google_health.data_types import REGISTRY
    from app.services.archive.storage import store_records
    from app.services.dashboard_views.dashboard_workouts import create_workout
    from app.services.readers.dashboard_manual import create_measurement

    settings = get_settings()
    engine = initialize_sessions(settings, SessionLocal)
    now = datetime.now(UTC)
    # Two days back, so the day is complete in every time zone the browser may run in.
    latest = now.date() - timedelta(days=2)
    days = [latest - timedelta(days=offset) for offset in range(HISTORY_DAYS)]
    batches = {
        "steps": [
            steps(day, MARKER_STEPS if day == latest else 4000 + index * 37)
            for index, day in enumerate(days)
        ],
        "sleep": [sleep(day) for day in days],
        "daily-resting-heart-rate": [
            resting(day, 55 + index % 9) for index, day in enumerate(days)
        ],
    }
    async with SessionLocal.begin() as session:
        for name, records in batches.items():
            await store_records(session, REGISTRY[name], records)
        for index in range(MEASUREMENTS):
            when = now - timedelta(days=index + 1)
            await create_measurement(
                session,
                {"metric": "weight", "value": Decimal(760 + index) / 10, "measured_at": when},
            )
        for index in range(WORKOUTS):
            start = now - timedelta(days=2 * index + 2, hours=3)
            await create_workout(
                session,
                {
                    "exercise_type": "running",
                    "start_time": start,
                    "end_time": start + timedelta(minutes=45),
                    "calories_kcal": Decimal(400 + index),
                    "distance_meters": Decimal(5000 + index),
                },
            )
    if engine is not None:
        await engine.dispose()
    return {"latest_day": latest.isoformat(), "marker_steps": MARKER_STEPS}


if __name__ == "__main__":
    migrate()
    Path(sys.argv[1]).write_text(json.dumps(asyncio.run(seed())), encoding="utf-8")
