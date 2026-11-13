"""A history page reads only its metric, and answers exactly what the dashboard shows."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from test_garmin_normalize import DAY, archive_day
from test_metric_history import garmin_records, records

import app.services.dashboard_views.dashboard as dashboard_service
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_history import history_data
from app.services.readers.dashboard_queries import DashboardQuery

TODAY = DAY + timedelta(days=1)
METRICS = {"steps": "steps", "sleep": "sleep_seconds", "heart": "resting_bpm"}
# Tables only the other metrics, the profile or the charts read.
ELSEWHERE = {
    "steps": {"sleep_sessions", "resting_heart_rate", "heart_rate", "manual_measurements"},
    "sleep": {"steps", "resting_heart_rate", "heart_rate", "manual_measurements"},
    "heart": {"steps", "sleep_sessions", "heart_rate", "manual_measurements"},
}


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime.combine(TODAY, datetime.min.time(), UTC) + timedelta(hours=12)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


@pytest.fixture
async def archive(sessions, monkeypatch):
    monkeypatch.setattr(dashboard_service, "datetime", Frozen)
    await archive_day(sessions)
    async with sessions.begin() as session:
        for metric in METRICS:
            session.add_all(
                records(metric, DAY - timedelta(days=2), f"{metric}-before")
                + records(metric, DAY, f"{metric}-same-day")
                + records(metric, DAY - timedelta(days=3), f"{metric}-gone", deleted=True)
                + records(metric, DAY - timedelta(days=3), f"{metric}-bad", quarantined=True)
            )
    return sessions


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("timezone", ["UTC", "Europe/Istanbul"])
async def test_a_page_is_the_dashboard_for_its_metric(archive, metric, timezone):
    async with archive() as session:
        page = await history_data(session, metric, 1, timezone, TODAY)
    async with archive() as session:
        panel = await dashboard_data(session, query=DashboardQuery(TODAY, 30, timezone))
    assert page["start_date"] == panel["start_date"]
    assert page["end_date"] == panel["end_date"]
    expected = [
        {"date": day["date"], "value": day[METRICS[metric]], "partial": day["partial"]}
        for day in panel["days"]
    ]
    assert page["days"] == expected
    assert any(day["value"] is not None for day in page["days"])


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("archive_days", [0, 100])
async def test_a_page_reads_its_metric_alone(archive, metric, archive_days):
    async with archive.begin() as session:
        for offset in range(31, 31 + archive_days):
            session.add_all(garmin_records(metric, TODAY - timedelta(days=offset)))
    engine = archive.kw["bind"].sync_engine
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        async with archive() as session:
            await history_data(session, metric, 1, "UTC", TODAY)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    read = {
        table
        for statement in statements
        for table in re.findall(r"(?:FROM|JOIN)\s+(\w+)", statement)
    }
    assert not read & ELSEWHERE[metric], read
    # Boundary aggregates (Google, Garmin, Apple), then one page read per archive: Google, Garmin,
    # sample data and the GitHub-only Apple extension.
    assert len(statements) == 7, statements
    assert len(statements) <= 8, statements
    # Both boundary queries aggregate in SQL; only the selected page loads payloads.
    for statement in statements[:2]:
        assert "min(" in statement and "max(" in statement
        assert "raw_json" not in statement
    assert all(
        "heart_rate_variability" not in statement
        and "oxygen_saturation" not in statement
        and "weight" not in statement
        and "scalar_metrics" not in statement
        and "exercise_sessions" not in statement
        for statement in statements
    )
