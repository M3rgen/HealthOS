"""Empty civil-day cards shared by provider projections and the dashboard."""

from datetime import date, timedelta

from app.schemas.dashboard import DashboardDay

type Platforms = dict[date, dict[str, set[str | None]]]


DAILY_METRICS = ("steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2")


def day_rows(first: date, days: int, today: date) -> tuple[dict[date, DashboardDay], Platforms]:
    rows: dict[date, DashboardDay] = {
        first + timedelta(days=i): {
            "date": (first + timedelta(days=i)).isoformat(),
            "steps": None,
            "sleep_seconds": None,
            "sleep_assumptions": [],
            "resting_bpm": None,
            "hrv_ms": None,
            "spo2": None,
            "partial": first + timedelta(days=i) == today,
            "sources": {},
            "status": "empty",
        }
        for i in range(days)
    }
    # day -> metric -> the Google platforms whose records made the value (the source label).
    platforms: Platforms = {day: {} for day in rows}

    return rows, platforms
