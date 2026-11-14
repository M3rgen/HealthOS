"""Synthetic sleep evidence and one check of a daily value through every dashboard output.

Shared by the numeric-limit and production-ingestion tests: the sleep fields the asleep reading
uses, a payload builder for one field, an API client with a fixed clock, and a helper that reads a
metric through the service, HTTP, history, CSV and both Umay consumers.
"""

import csv
import io
import json
from datetime import datetime, timedelta

import httpx
import pytest
from derived_records import DAY, T0, snapshot
from fastapi import FastAPI

from app.api import dashboard
from app.db.session import get_session
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.umay import codex_health

# Evidence the asleep reading uses; the subtraction never reads the other stage totals.
SLEEP_FIELDS = (
    "summary",
    "duration",
    "summary_awake",
    "summary_restless",
    "stage_awake",
    "stage_wake",
    "stage_restless",
    "wrapped_durationSeconds",
)


def evidence(field, value, start=T0):
    if field == "summary":
        return {
            "summary": {"minutesAsleep": value},
            "stages": [{"type": "AWAKE", "duration": "0s"}],
        }
    if field == "duration":
        return {"duration": value + "s", "stages": [{"type": "AWAKE", "duration": "0s"}]}
    if field.startswith("summary_"):
        return {"summary": {"stagesSummary": [{"type": field[8:], "minutes": value}]}}
    entry = {
        "type": field.removeprefix("stage_"),
        "duration": value + "s",
        "startTime": start.isoformat(),
        "endTime": (start + timedelta(hours=8)).isoformat(),
    }
    if field == "wrapped_durationSeconds":
        entry["type"] = "AWAKE"
        entry["durationSeconds"] = value
        del entry["duration"]
        return {"sleepStages": {"data": [entry]}}
    return {"stages": [entry]}


@pytest.fixture
async def api(sessions, monkeypatch):
    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T0 + timedelta(days=len(SLEEP_FIELDS) + 3)).astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Now)
    app = FastAPI()
    app.include_router(dashboard.router)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


async def outputs(api, sessions, key, expected, metric, kind):
    """Exercise the shared calculation through HTTP, CSV and both Umay consumers."""
    last = DAY + timedelta(days=len(expected) - 1)
    query = {"start_date": str(DAY), "end_date": str(last)}
    params = {"end_date": query["end_date"], "days": len(expected)}
    async with sessions() as session:
        before = await snapshot(session)
        data = await dashboard_data(session, query=DashboardQuery(last, len(expected), "UTC"))
        assert [row[key] for row in reversed(data["days"])] == expected
    response = await api.get("/api/dashboard", params=params)
    assert response.status_code == 200, response.text
    assert [row[key] for row in reversed(response.json()["days"])] == expected
    history = await api.get("/api/dashboard/history", params={"metric": metric})
    assert history.status_code == 200, history.text
    dates = {row["date"]: row["value"] for row in history.json()["days"]}
    assert [dates[str(DAY + timedelta(days=i))] for i in range(len(expected))] == expected
    exported = await api.get("/api/dashboard/export.csv", params=params)
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert [row[key] for row in reversed(rows)] == [
        "" if value is None else str(value) for value in expected
    ]
    assert not any(word in exported.text.lower() for word in ("nan", "inf"))
    daily = await codex_health.read_health(sessions, {"action": "daily", **query})
    assert [row[key] for row in reversed(daily["days"])] == expected
    card = await codex_health.read_health(
        sessions, {"action": "coverage", "data_type": kind, **query}
    )
    [coverage] = card["metrics"]
    assert coverage["value_days"] == sum(value is not None for value in expected)
    assert coverage["days_without_value"] == sum(value is None for value in expected)
    # Reject non-standard JSON constants anywhere, including the derived daily result.
    json.dumps(daily, allow_nan=False)
    json.dumps(card, allow_nan=False)
    async with sessions() as session:
        assert await snapshot(session) == before
    return daily
