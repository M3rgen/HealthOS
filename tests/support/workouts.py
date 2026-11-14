"""Workout test helpers: a synthetic exercise record."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from derived_records import point

START = datetime(2026, 8, 31, 21, 30, tzinfo=UTC)
OPTIONAL_METRICS = {"caloriesKcal": "calories_kcal", "distanceMillimeters": "distance_meters"}
METRIC_VALUES = [
    -10,
    "-10",
    -1000,
    -5e-324,
    "-1e-999999",
    "-1e30",
    0,
    "0",
    "-0",
    -0.0,
    12.5,
    "12.5",
]


def exercise(name="one", start=START, **fields):
    payload = {
        "interval": {
            "startTime": start.isoformat(),
            "endTime": (start + timedelta(hours=1)).isoformat(),
        },
        "exerciseType": "RUNNING",
        "activeDuration": "3500s",
        "duration": "3600s",
        "metricsSummary": {"caloriesKcal": 123, "distanceMillimeters": 4567000},
        **fields,
    }
    return point("exercise", name, payload)
