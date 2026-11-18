"""Step archive bounds read the archive's two ends; the work does not grow with its history.

Before the windows, the bounds streamed every step row's payload (with its eligibility probes)
and sorted the whole archive. The archive's history grows tenfold here; the rows and buffers
the bounds statements touch must not, while the full read's grow with it. The result still
equals the full read's. Every row is generated: fifteen-minute intervals with a constant count.
"""

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_projection_archive import analyze
from sqlalchemy import event, text
from test_google_step_bounds_windows import reference_bounds

from app.services.interpretation import google_daily_readers as reader
from app.services.readers.reading import ArchiveBounds

pytestmark = pytest.mark.integration
TODAY = date(2026, 1, 10)
ZONE = ZoneInfo("Europe/Brussels")
NEWEST = datetime(2026, 1, 10, 9, tzinfo=UTC)
# One raw row and its normalized row per quarter hour; the payload is a valid step interval.
ROWS = """
WITH generated AS (
    SELECT t, 'synthetic/steps/' || CAST(:prefix AS text) || extract(epoch FROM t)::bigint AS key
    FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                         interval '15 minutes') t
), raw AS (
    INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                                 first_seen_at, last_seen_at, source_start_time, source_end_time)
    SELECT gen_random_uuid(), key, 'steps',
           jsonb_build_object(
               'name', key,
               'dataSource', jsonb_build_object('platform', 'FITBIT'),
               'steps', jsonb_build_object(
                   'count', CAST(:count AS integer),
                   'interval', jsonb_build_object(
                       'startTime', to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                       'endTime', to_char((t + interval '15 minutes') AT TIME ZONE 'UTC',
                                          'YYYY-MM-DD"T"HH24:MI:SS"Z"')))),
           'x', t, t, t, t + interval '15 minutes'
    FROM generated
)
INSERT INTO steps (id, google_data_point_id, start_time, end_time, count)
SELECT gen_random_uuid(), key, t, t + interval '15 minutes', 10 FROM generated
"""


async def add(session, prefix, first, last, count=10):
    await session.execute(
        text(ROWS), {"prefix": prefix, "first": first, "last": last, "count": count}
    )


def nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", ()):
        yield from nodes(child)


def archive_reads(plan: dict) -> dict:
    """Rows fetched from the payload table, and whole-table scans of either step table.

    raw_health_data holds every payload (the table the full read made slow). The planner may
    hash the small normalized steps table instead of probing it by key once a window holds
    many rows; that is a cost choice on narrow rows, counted separately.
    """
    rows = raw_scans = steps_scans = 0
    for node in nodes(plan["Plan"]):
        relation = node.get("Relation Name")
        if relation == "raw_health_data":
            loops = node.get("Actual Loops", 0)
            rows += loops * (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0))
        if node["Node Type"] == "Seq Scan":
            raw_scans += relation == "raw_health_data"
            steps_scans += relation == "steps"
    return {"raw_rows": round(rows), "raw_seq_scans": raw_scans, "steps_seq_scans": steps_scans}


async def measured(sessions, read):
    """Run ``read`` and EXPLAIN (ANALYZE, BUFFERS) each SELECT it sent, with its parameters."""
    async with sessions() as session:
        connection = await session.connection()
        sent = []

        def capture(conn, cursor, statement, parameters, context, many):
            if statement.lstrip().startswith("SELECT"):
                sent.append((statement, parameters))

        event.listen(connection.sync_connection, "before_cursor_execute", capture)
        try:
            result = await read(session)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", capture)
        totals = {"statements": len(sent), "raw_rows": 0, "raw_seq_scans": 0, "steps_seq_scans": 0}
        for statement, parameters in sent:
            found = (
                await connection.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", parameters
                )
            ).scalar_one()
            reads = archive_reads((json.loads(found) if isinstance(found, str) else found)[0])
            for key, value in reads.items():
                totals[key] += value
    return result, totals


async def test_step_bounds_read_only_the_archive_ends(pg_sessions):
    async with pg_sessions.begin() as session:
        await add(session, "recent:", NEWEST - timedelta(days=60), NEWEST)
    windows, full, history = {}, {}, NEWEST - timedelta(days=60)
    for days in (60, 600):
        async with pg_sessions.begin() as session:
            first = NEWEST - timedelta(days=days)
            if first < history:
                await add(session, f"old{days}:", first, history - timedelta(minutes=15))
                history = first
        await analyze(pg_sessions.kw["bind"])
        expected = ArchiveBounds(first.astimezone(ZONE).date(), NEWEST.astimezone(ZONE).date())
        bounds, windows[days] = await measured(
            pg_sessions, lambda session: reader.archive_bounds(session, "steps", TODAY, ZONE)
        )
        reference, full[days] = await measured(
            pg_sessions, lambda session: reference_bounds(session, TODAY, ZONE)
        )
        assert bounds == reference == expected
    print({"windows": windows, "full": full})
    # The anchor and one window at each end, whatever the history: the same payload rows,
    # read through index ranges; at this size the steps table is probed by key too.
    assert windows[60]["statements"] == windows[600]["statements"] == 3, windows
    assert windows[600]["raw_rows"] == windows[60]["raw_rows"], windows
    assert windows[60]["raw_seq_scans"] == windows[600]["raw_seq_scans"] == 0, windows
    assert windows[600]["steps_seq_scans"] == 0, windows
    # The measure can see growth: the full read's rows follow the history.
    assert full[600]["raw_rows"] > 5 * full[60]["raw_rows"], full
    assert windows[600]["raw_rows"] * 50 < full[600]["raw_rows"], (windows, full)


async def test_widening_reads_grow_with_the_gap_not_the_archive(pg_sessions):
    """Rejected payloads at both ends make the windows widen; each widening doubles."""
    async with pg_sessions.begin() as session:
        await add(session, "history:", NEWEST - timedelta(days=600), NEWEST)
    async with pg_sessions.begin() as session:
        # The oldest and the newest twenty days of counts are no longer measurements.
        await session.execute(
            text(
                "UPDATE raw_health_data SET raw_json = jsonb_set(raw_json, '{steps,count}', '-1') "
                "WHERE data_type = 'steps' AND (source_start_time < :old OR source_start_time "
                ">= :new)"
            ),
            {"old": NEWEST - timedelta(days=580), "new": NEWEST - timedelta(days=20)},
        )
    await analyze(pg_sessions.kw["bind"])
    bounds, windows = await measured(
        pg_sessions, lambda session: reader.archive_bounds(session, "steps", TODAY, ZONE)
    )
    reference, full = await measured(
        pg_sessions, lambda session: reference_bounds(session, TODAY, ZONE)
    )
    print({"windows": windows, "full": full})
    assert bounds == reference
    # Windows of 4, 8, 16 and 32 days at each end, plus the anchor.
    assert windows["statements"] == 1 + 4 + 4, windows
    assert windows["raw_seq_scans"] == 0, windows
    # 2 * (4 + 8 + 16 + 32) days of payloads at most, of 600: about a fifth.
    assert windows["raw_rows"] * 4 < full["raw_rows"], (windows, full)
