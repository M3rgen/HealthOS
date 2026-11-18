"""A history page reads only its metric, and answers exactly what the dashboard shows."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from derived_records import sleep, steps, store, summary
from history_cache_archive import REVISION_READS
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


def step_bounds_statements(statements, batch_sizes, reads):
    """The first/newest start, the oldest window, then (unless it held every start) the newest
    window, each validating payloads in bounded batches; then the Garmin bounds."""
    assert statements[0].startswith("SELECT min(raw_health_data.source_start_time)")
    for index in range(1, 1 + reads):
        assert statements[index].startswith("SELECT raw_health_data.raw_json")
        assert "source_end_time > raw_health_data.source_start_time" in statements[index]
        assert batch_sizes[index] == 1000
    assert "raw_health_data.source_start_time >=" not in statements[1]
    if reads == 2:
        assert "coalesce(raw_health_data.source_end_time" in statements[2]
    fields = statements[1 + reads].split("\nFROM", 1)[0]
    assert fields.startswith("SELECT steps.source_record_id, steps.count, ")
    assert "garmin_raw_records.body -> " in fields
    assert batch_sizes[1 + reads] == 1000


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("archive_days", [0, 100])
async def test_a_page_reads_its_metric_alone(archive, metric, archive_days):
    if metric == "steps":
        for offset in range(archive_days):
            start = datetime.combine(TODAY, datetime.min.time(), UTC) - timedelta(days=offset + 31)
            await store(
                archive, "steps", steps(str(offset), start=start, end=start + timedelta(days=2))
            )
    if metric == "heart":
        await store(
            archive,
            "daily-resting-heart-rate",
            *(
                summary(
                    "daily-resting-heart-rate",
                    str(offset),
                    TODAY - timedelta(days=offset),
                    beatsPerMinute=60,
                )
                for offset in range(31, 31 + archive_days)
            ),
        )
    if metric == "sleep":
        await store(
            archive,
            "sleep",
            *(
                sleep(
                    str(offset),
                    start=datetime.combine(TODAY, datetime.min.time(), UTC)
                    - timedelta(days=offset, hours=8),
                    end=datetime.combine(TODAY, datetime.min.time(), UTC) - timedelta(days=offset),
                )
                for offset in range(31, 31 + archive_days)
            ),
        )
    async with archive.begin() as session:
        for offset in range(31, 31 + archive_days):
            session.add_all(garmin_records(metric, TODAY - timedelta(days=offset)))
    engine = archive.kw["bind"].sync_engine
    statements: list[str] = []
    revisions: list[str] = []
    batch_sizes: list[int | None] = []

    def record(connection, cursor, statement, parameters, context, many):
        if statement in REVISION_READS:
            revisions.append(statement)
        elif statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
            batch_sizes.append(context.execution_options.get("yield_per"))

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
    assert len(revisions) == 2  # Before and after the unchanged bounded streaming miss.
    # Boundary reads (Google, Garmin, Apple), then one page read per archive: Google, Garmin,
    # sample data and the GitHub-only Apple extension.
    # Google step bounds read the archive's first and newest start, then one window at each
    # end (each end here has a valid day in its first window), or one read when the first
    # window already holds every start (the four-day archive): one or two more statements.
    steps_reads = 2 if archive_days else 1
    google_bounds = 1 + steps_reads if metric == "steps" else 1
    assert len(statements) == google_bounds + 6, statements
    # Apple's boundary query (after the Garmin bounds) aggregates in SQL and loads no payload.
    apple_bounds = statements[google_bounds + 1]
    assert "min(" in apple_bounds and "max(" in apple_bounds
    assert "raw_json" not in apple_bounds
    # Google bounds validate current payloads in bounded batches.
    if metric == "sleep":
        assert (
            "raw_json" in statements[0]
            and "ORDER BY raw_health_data_1.source_end_time" in statements[0]
        )
        assert batch_sizes[0] == 100
    elif metric == "heart":
        assert statements[0].startswith(
            "SELECT raw_health_data.data_type, raw_health_data.raw_json"
        )
        assert (
            "ORDER BY raw_health_data.source_start_time, raw_health_data.last_seen_at"
            in statements[0]
        )
        assert batch_sizes[0] == 1000
    if metric == "steps":
        step_bounds_statements(statements, batch_sizes, steps_reads)
    if metric == "sleep":
        for index in (1, 4, 5):  # Garmin bounds, Garmin page, DEMO page.
            fields = statements[index].split("\nFROM", 1)[0]
            assert fields.count(".body -> ") == 4
            assert "sleep.body," not in fields and "sleep_summaries.body," not in fields
            assert batch_sizes[index] == 1000
    if metric == "heart":
        fields = statements[1].split("\nFROM", 1)[0]
        assert fields.startswith(
            "SELECT resting_heart_rate.source_record_id, resting_heart_rate.bpm, "
        )
        assert fields.count(".body -> ") == 2
        assert batch_sizes[1] == 1000
    assert all(
        "heart_rate_variability" not in statement
        and "oxygen_saturation" not in statement
        and "weight" not in statement
        and "scalar_metrics" not in statement
        and "exercise_sessions" not in statement
        for statement in statements
    )
