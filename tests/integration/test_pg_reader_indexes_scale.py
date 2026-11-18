"""A past window's reads stay the same size however long the archive grows (20261116_0041).

Before these indexes an older step window fetched every step row from its start up to the
newest one, each superseded-step probe landed somewhere in the links of every data type, and
a Garmin read scanned the day's Google heart rows or the whole SpO2/HRV table. Here the
archive grows tenfold on both sides of a fixed window (older history and newer days, more
Google rows next to the same Garmin ones); the rows the plans touch must not grow, and the
probes must use the small indexes. Synthetic rows only.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from garmin_projection_archive import analyze, plan_summary
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects.postgresql import asyncpg as postgresql_asyncpg

from app.services.interpretation import google_daily_readers
from app.services.providers import garmin_daily_values, garmin_heart_values
from app.services.readers import CalendarRead, DashboardSources, daily_window

pytestmark = pytest.mark.integration
WINDOW_END = date(2026, 3, 15)
ZONE = ZoneInfo("Europe/Brussels")
STEP_ROWS = """
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time, source_end_time)
SELECT gen_random_uuid(), CAST(:prefix AS text) || n, 'steps', '{}', 'x', t, t, t,
       t + interval '15 minutes'
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '15 minutes') WITH ORDINALITY AS g(t, n)
"""
NORMALIZED = """
INSERT INTO steps (id, google_data_point_id, start_time, end_time, count)
SELECT gen_random_uuid(), google_data_point_id, source_start_time, source_end_time, 10
FROM raw_health_data r
WHERE data_type = 'steps' AND google_data_point_id LIKE CAST(:prefix AS text) || '%'
"""
# Every third row of a batch was archived again under a new key: the old row is superseded.
LINKS = """
INSERT INTO raw_health_data_supersessions (old_raw_id, new_raw_id, data_type,
    old_google_data_point_id, new_google_data_point_id, reason, old_fingerprint,
    new_fingerprint, same_reading, new_payload_hash)
SELECT o.id, n.id, 'steps', o.google_data_point_id, n.google_data_point_id, 'duplicate',
       'fp', 'fp', true, 'x'
FROM raw_health_data o JOIN raw_health_data n
  ON n.data_type = 'steps' AND n.google_data_point_id = 'again-' || o.google_data_point_id
WHERE o.data_type = 'steps' AND o.google_data_point_id LIKE CAST(:prefix AS text) || '%'
"""
AGAIN = """
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time, source_end_time)
SELECT gen_random_uuid(), 'again-' || google_data_point_id, data_type, raw_json, 'y',
       first_seen_at, last_seen_at, source_start_time, source_end_time
FROM raw_health_data
WHERE data_type = 'steps' AND google_data_point_id LIKE CAST(:prefix AS text) || '%'
  AND right(google_data_point_id, 1) IN ('0', '3', '6', '9')
"""
# Other data types' links fill the old key index around the step links.
OTHER_LINKS = """
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time)
SELECT gen_random_uuid(), CAST(:prefix AS text) || n || side, 'heart-rate', '{}', 'x',
       now(), now(), now()
FROM generate_series(1, CAST(:count AS integer)) n, (VALUES ('a'), ('b')) s(side);
INSERT INTO raw_health_data_supersessions (old_raw_id, new_raw_id, data_type,
    old_google_data_point_id, new_google_data_point_id, reason, old_fingerprint,
    new_fingerprint, same_reading, new_payload_hash)
SELECT a.id, b.id, 'heart-rate', a.google_data_point_id, b.google_data_point_id, 'duplicate',
       'fp', 'fp', true, 'x'
FROM raw_health_data a JOIN raw_health_data b
  ON b.data_type = 'heart-rate' AND b.google_data_point_id = left(a.google_data_point_id, -1) || 'b'
WHERE a.data_type = 'heart-rate' AND a.google_data_point_id LIKE CAST(:prefix AS text) || '%a'
"""
GOOGLE_HEART = """
INSERT INTO heart_rate (id, google_data_point_id, provider, timestamp, bpm)
SELECT gen_random_uuid(), CAST(:prefix AS text) || n, 'GOOGLE', t, 60
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     CAST(:step AS interval)) WITH ORDINALITY AS g(t, n)
"""
GOOGLE_SPO2 = """
INSERT INTO oxygen_saturation (id, google_data_point_id, provider, timestamp, spo2)
SELECT gen_random_uuid(), CAST(:prefix AS text) || n, 'GOOGLE', t, 97
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '10 minutes') WITH ORDINALITY AS g(t, n)
"""
GARMIN = """
INSERT INTO heart_rate (id, google_data_point_id, provider, source_record_id, timestamp, bpm)
SELECT gen_random_uuid(), 'garmin:heart-rate:' || d::date || ':' || n || '\\:v2', 'GARMIN',
       'heart-rate:' || d::date, d + n * interval '2 minutes', 55
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '1 day') d, generate_series(0, 9) n;
INSERT INTO oxygen_saturation (id, google_data_point_id, provider, source_record_id,
                               timestamp, spo2)
SELECT gen_random_uuid(), 'garmin:spo2:' || d::date || '\\:v2', 'GARMIN', 'spo2:' || d::date, d, 96
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '1 day') d
"""


class Captured(Exception):
    pass


class Capture:
    """A session stand-in that hands back the reader's first statement unexecuted."""

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def stream_scalars(self, statement, **_):
        raise Captured(statement)

    stream = execute = stream_scalars


async def statement_of(call):
    try:
        await call
    except Captured as captured:
        return captured.args[0]
    raise AssertionError("the reader sent no statement")


def nodes(plan):
    yield plan
    for child in plan.get("Plans", ()):
        yield from nodes(child)


async def explained(session, statement) -> dict:
    connection = await session.connection()
    sync = connection.sync_connection

    def prefixed(conn, cursor, sql, parameters, context, executemany):
        return f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", parameters

    event.listen(sync, "before_cursor_execute", prefixed, retval=True)
    try:
        result = await connection.stream(statement)  # some readers ask for a cursor
        found = (await result.fetchone())[0]
        await result.close()
    finally:
        event.remove(sync, "before_cursor_execute", prefixed)
    plan = (json.loads(found) if isinstance(found, str) else found)[0]
    # Executed nodes only: a part ruled out by its one-time filter is planned, never run.
    every = [node for node in nodes(plan["Plan"]) if node.get("Actual Loops", 0)]
    append = next((node for node in nodes(plan["Plan"]) if node["Node Type"] == "Append"), None)
    return {
        # Which parts of a union ran: a part ran when any node below its gate did.
        "parts": [
            any(
                node.get("Actual Loops", 0)
                for child in part.get("Plans", ())
                if child.get("Parent Relationship") != "InitPlan"  # the gate's own test
                for node in nodes(child)
            )
            for part in (append["Plans"] if append else ())
        ],
        **plan_summary(plan),
        "indexes": {node["Index Name"] for node in every if "Index Name" in node},
        "scanned": {node["Relation Name"] for node in every if node["Node Type"] == "Seq Scan"},
    }


async def readers(session) -> dict:
    window = daily_window(WINDOW_END, 7, ZONE)
    calendar = CalendarRead(WINDOW_END, ZONE, DashboardSources())
    statements = {
        "steps": await statement_of(
            google_daily_readers.read_steps(Capture(), window, {}, calendar=calendar)
        ),
        "heart": await statement_of(
            garmin_heart_values.heart_values(Capture(), window.start, window.stop, "GARMIN")
        ),
        "spo2": garmin_daily_values._query("spo2", window.first, WINDOW_END, "GARMIN"),
    }
    await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
    await session.execute(text("SET LOCAL jit = off"))
    return {name: await explained(session, statement) for name, statement in statements.items()}


async def grow(session, label: str, first: datetime, last: datetime, garmin=False) -> None:
    prefix = f"{label}:"
    for statement in (STEP_ROWS, AGAIN, NORMALIZED):
        await session.execute(text(statement), {"prefix": prefix, "first": first, "last": last})
    await session.execute(text(NORMALIZED), {"prefix": f"again-{prefix}"})
    await session.execute(text(LINKS), {"prefix": prefix})
    days = (last - first).days
    for part in OTHER_LINKS.split(";"):
        await session.execute(text(part), {"prefix": f"h-{label}:", "count": days * 50})
    await session.execute(
        text(GOOGLE_HEART),
        {"prefix": f"gh-{label}:", "first": first, "last": last, "step": timedelta(minutes=5)},
    )
    await session.execute(
        text(GOOGLE_SPO2), {"prefix": f"gs-{label}:", "first": first, "last": last}
    )
    if garmin:  # the same Garmin rows throughout: only the Google rows grow
        for part in GARMIN.split(";"):
            await session.execute(text(part), {"first": first, "last": last})


async def test_a_past_window_reads_the_same_rows_however_long_the_archive(pg_sessions):
    end = datetime.combine(WINDOW_END, datetime.min.time(), UTC)
    found = {}
    async with pg_sessions.begin() as session:
        await grow(session, "near", end - timedelta(days=30), end + timedelta(days=30), True)
    for label, before, after in (("small", 0, 0), ("grown", 300, 300)):
        if before:
            async with pg_sessions.begin() as session:
                await grow(
                    session,
                    f"{label}-old",
                    end - timedelta(days=30 + before),
                    end - timedelta(days=31),
                )
                await grow(
                    session,
                    f"{label}-new",
                    end + timedelta(days=31),
                    end + timedelta(days=30 + after),
                )
                # Ten times the Google heart rows on the window's own days.
                await session.execute(
                    text(GOOGLE_HEART),
                    {
                        "prefix": "dense:",
                        "first": end - timedelta(days=8),
                        "last": end + timedelta(days=1),
                        "step": timedelta(seconds=30),
                    },
                )
        await analyze(pg_sessions.kw["bind"])
        async with pg_sessions.begin() as session:
            found[label] = await readers(session)
    print(
        {
            label: {name: plan["work"] for name, plan in plans.items()}
            for label, plans in found.items()
        }
    )
    for name in ("steps", "heart", "spo2"):
        small, grown = found["small"][name], found["grown"][name]
        assert grown["rows"] == small["rows"], name
        assert grown["work"] <= small["work"] * 1.1 + 50, (name, small, grown)
        assert not {"raw_health_data", "heart_rate", "oxygen_saturation"} & grown["scanned"], name
    steps = found["grown"]["steps"]["indexes"]
    assert {
        "ix_raw_health_data_long_steps",
        "ix_raw_health_data_supersessions_type_keys",
    } <= steps
    assert "ix_heart_rate_provider_time" in found["grown"]["heart"]["indexes"]
    assert "ix_oxygen_saturation_provider_record" in found["grown"]["spo2"]["indexes"]


EDGE_ROWS = """
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time, source_end_time)
SELECT gen_random_uuid(), 'edge-' || n, 'steps', '{}', 'x', now(), now(), s, e
FROM (VALUES
  (1, timestamptz '-infinity', timestamptz '2026-03-10T00:00:00Z'),
  (2, timestamptz '2026-03-09T00:00:00Z', timestamptz 'infinity'),
  (3, timestamptz '-infinity', timestamptz 'infinity'),
  (4, timestamptz '2026-03-10T00:00:00Z', NULL),
  (5, timestamptz '2026-03-10T05:00:00Z', timestamptz '2026-03-10T04:00:00Z'),
  (6, timestamptz '2026-03-14T23:00:00Z', timestamptz '2026-03-15T23:00:00Z'),
  (7, timestamptz '2026-03-14T23:00:00Z', timestamptz '2026-03-15T23:00:00.000001Z'),
  (8, timestamptz '2026-02-01T00:00:00Z', timestamptz '2026-03-09T00:00:00Z'),
  (9, timestamptz '2026-03-01T00:00:00Z', timestamptz '2026-03-08T00:00:01Z'),
  (10, timestamptz '2026-03-14T22:00:00Z', timestamptz '2026-03-14T22:00:00Z'),
  (11, timestamptz '2026-03-07T23:00:00Z', timestamptz '2026-03-08T00:00:00Z'),
  (12, timestamptz '1-01-01T00:00:00Z', timestamptz '9999-12-31T00:00:00Z'),
  (13, timestamptz '4713-01-01 00:00:00+00 BC', timestamptz '294276-12-31 00:00:00+00'),
  (14, timestamptz '294276-12-31 00:00:00+00', timestamptz '4713-01-01 00:00:00+00 BC'),
  (15, timestamptz '0001-01-01 00:00:00+00 BC', timestamptz '2026-03-14T00:00:00Z')
) AS v(n, s, e)
"""


async def test_the_two_parts_select_exactly_the_window_including_unmeasurable_rows(pg_sessions):
    """Infinite, missing, reversed, exactly-a-day and longer bounds, and bounds so far apart
    that their difference overflows an interval: every such row can be written under the
    long-step index, and the parts select what the window selects."""
    from sqlalchemy import select, union_all

    from app.models.state import RawHealthData
    from app.services.readers.measurement_windows import raw_interval_window, raw_step_window_parts

    async with pg_sessions.begin() as session:
        await session.execute(text(EDGE_ROWS))
        await session.execute(
            text(STEP_ROWS),
            {
                "prefix": "dense:",
                "first": datetime(2026, 3, 1, tzinfo=UTC),
                "last": datetime(2026, 3, 20, tzinfo=UTC),
            },
        )
    await analyze(pg_sessions.kw["bind"])
    for days in (1, 7, 31):
        window = daily_window(WINDOW_END, days, ZONE)
        key = RawHealthData.google_data_point_id
        steps = RawHealthData.data_type == "steps"
        async with pg_sessions() as session:
            expected = set(
                await session.scalars(
                    select(key).where(
                        steps,
                        *raw_interval_window("steps", RawHealthData, window.start, window.stop),
                    )
                )
            )
            parts = raw_step_window_parts(RawHealthData, window.start, window.stop)
            found = list(
                await session.scalars(
                    union_all(*(select(key).where(steps, *part) for part in parts))
                )
            )
        assert len(found) == len(set(found)), days  # the parts are disjoint
        assert set(found) == expected, days
        if days == 31:  # unmeasurable rows still count
            assert {"edge-1", "edge-2", "edge-3", "edge-8", "edge-9", "edge-13"} <= expected


async def _reads(sessions, end: date, days: int) -> dict:
    """``read_steps``'s per-platform result."""
    async with sessions() as session:
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
        reads: dict = {}
        calendar = CalendarRead(end, ZONE, DashboardSources())
        await google_daily_readers.read_steps(
            session, daily_window(end, days, ZONE), reads, calendar=calendar
        )
    return {source: (read.days, read.step_hours) for source, read in reads.items()}


async def _plan(sessions) -> dict:
    """The executed nodes of ``read_steps``'s statement for the fixed week."""
    window = daily_window(WINDOW_END, 7, ZONE)
    calendar = CalendarRead(WINDOW_END, ZONE, DashboardSources())
    statement = await statement_of(
        google_daily_readers.read_steps(Capture(), window, {}, calendar=calendar)
    )
    async with sessions() as session:
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
        await session.execute(text("SET LOCAL jit = off"))
        return await explained(session, statement)


async def test_without_a_valid_long_steps_index_the_window_stays_one_part(pg_sessions):
    """Left for later or left invalid by an interrupted build, the long steps' index cannot
    bound the long part, which would then read every step row: the statement runs the old
    one-part window instead (its parts are gated on the index), with the same result."""
    start = datetime(2026, 3, 1, tzinfo=UTC)
    async with pg_sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, "
                "payload_hash, first_seen_at, last_seen_at, source_start_time, source_end_time) "
                "SELECT gen_random_uuid(), 'w-' || n, 'steps', json_build_object('steps', "
                "json_build_object('count', n % 50, 'interval', json_build_object("
                "'startTime', to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), "
                "'endTime', to_char((t + interval '1 hour') AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')))), 'x', t, t, t, t + interval '1 hour' "
                "FROM generate_series(CAST(:first AS timestamptz), CAST(:first AS timestamptz) "
                "+ interval '20 days', interval '1 hour') WITH ORDINALITY AS g(t, n)"
            ),
            {"first": start},
        )
        await session.execute(
            text(
                "INSERT INTO steps (id, google_data_point_id, start_time, end_time, count) "
                "SELECT gen_random_uuid(), google_data_point_id, source_start_time, "
                "source_end_time, 1 FROM raw_health_data WHERE data_type = 'steps'"
            )
        )
    await analyze(pg_sessions.kw["bind"])
    expected = await _reads(pg_sessions, WINDOW_END, 7)
    plan = await _plan(pg_sessions)
    assert plan["parts"] == [True, True, False]
    assert "ix_raw_health_data_long_steps" in plan["indexes"]
    assert any(day for days, _hours in expected.values() for day in days)
    pages = await _history(pg_sessions)
    engine = create_engine(pg_sessions.kw["bind"].url.set(drivername="postgresql+psycopg"))
    try:
        for leftover in ("missing", "invalid"):
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.execute(text("DROP INDEX IF EXISTS ix_raw_health_data_long_steps"))
                if leftover == "invalid":  # what an interrupted build leaves under the name
                    with pytest.raises(Exception, match="could not create unique index"):
                        connection.execute(
                            text(
                                "CREATE UNIQUE INDEX CONCURRENTLY ix_raw_health_data_long_steps "
                                "ON raw_health_data (data_type) WHERE data_type = 'steps'"
                            )
                        )
            assert await _reads(pg_sessions, WINDOW_END, 7) == expected, leftover
            plan = await _plan(pg_sessions)
            # Only the old one-part window ran; the long part (no bound without its index) not.
            assert plan["parts"] == [False, False, True], leftover
            assert await _history(pg_sessions) == pages, leftover
    finally:
        engine.dispose()


async def _history(sessions) -> dict:
    """The step history page in each plan mode, read as on a bounds-cache miss and as on a
    hit, where nothing forces custom plans (the cached bounds are stood in for: another
    process's transactions can keep the process cache from filling), all equal."""
    from app.services.dashboard_views import dashboard_history

    today, found = date(2026, 3, 22), {}
    original = dashboard_history.read_bounds
    try:
        for mode in ("auto", "force_custom_plan", "force_generic_plan"):
            dashboard_history.read_bounds = original
            async with sessions() as session:
                await session.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
                found[mode, "miss"] = await dashboard_history.history_data(
                    session, "steps", 1, "UTC", today, use_bounds_cache=False
                )
            async with sessions() as session:
                bounds = await original(
                    session, "steps", today, ZoneInfo("UTC"), dashboard_history.ARCHIVES
                )

            async def cached(*_args, _bounds=bounds, **_kwargs):
                return _bounds

            dashboard_history.read_bounds = cached
            async with sessions() as session:
                await session.execute(text(f"SET LOCAL plan_cache_mode = {mode}"))
                found[mode, "cached"] = await dashboard_history.history_data(
                    session, "steps", 1, "UTC", today
                )
    finally:
        dashboard_history.read_bounds = original
    reference = found["force_custom_plan", "miss"]
    assert any(day["value"] for day in reference["days"])
    for key, page in found.items():
        assert page == reference, key
    return reference


YEARS = """
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time, source_end_time)
SELECT gen_random_uuid(), 'y-' || n, 'steps', '{}', 'x', t, t, t, t + interval '15 minutes'
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '4 hours') WITH ORDINALITY AS g(t, n);
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time, source_end_time)
SELECT gen_random_uuid(), 'yl-' || n, 'steps', '{}', 'x', t, t, t, t + interval '3 days'
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '1 month') WITH ORDINALITY AS g(t, n);
INSERT INTO steps (id, google_data_point_id, start_time, end_time, count)
SELECT gen_random_uuid(), google_data_point_id, source_start_time, source_end_time, 10
FROM raw_health_data WHERE data_type = 'steps' AND google_data_point_id LIKE 'y%';
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time)
SELECT gen_random_uuid(), 'h-' || n, 'heart-rate', '{}', 'x', t, t, t
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     interval '4 hours') WITH ORDINALITY AS g(t, n)
"""


def _literal(value) -> str:
    """A parameter of the step read as SQL (synthetic dates, type names and booleans only)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'::timestamptz"
    if isinstance(value, timedelta):
        return f"'{value.total_seconds()} seconds'::interval"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


async def _buffers(sessions, end: date, mode: str, parts=None) -> dict:
    """Shared buffers and executed scans of the step read for the week ending ``end``."""
    window = daily_window(end, 7, ZONE)
    calendar = CalendarRead(end, ZONE, DashboardSources())
    if parts is not None:
        original = google_daily_readers._step_window_parts
        google_daily_readers._step_window_parts = parts
    try:
        statement = await statement_of(
            google_daily_readers.read_steps(Capture(), window, {}, calendar=calendar)
        )
    finally:
        if parts is not None:
            google_daily_readers._step_window_parts = original
    # A prepared statement, as the driver's: under force_generic_plan it is planned once for
    # any parameters (EXPLAIN of a statement with parameters always plans for its values).
    compiled = statement.compile(
        dialect=postgresql_asyncpg.dialect(), compile_kwargs={"render_postcompile": True}
    )
    params = compiled.construct_params()
    arguments = ", ".join(_literal(params[name]) for name in compiled.positiontup)
    async with sessions() as session:
        connection = await session.connection()
        await connection.exec_driver_sql(f"SET LOCAL plan_cache_mode = {mode}")
        await connection.exec_driver_sql("SET LOCAL jit = off")
        await connection.exec_driver_sql(f"PREPARE step_read AS {compiled}")
        result = await connection.exec_driver_sql(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE step_read({arguments})"
        )
        found = result.scalar_one()
        await connection.exec_driver_sql("DEALLOCATE step_read")
    plan = (json.loads(found) if isinstance(found, str) else found)[0]["Plan"]
    every = [node for node in nodes(plan) if node.get("Actual Loops", 0)]

    def buffers(node):
        return node.get("Shared Hit Blocks", 0) + node.get("Shared Read Blocks", 0)

    # The index check of each part reads a few catalog pages: counted apart.
    gates = [
        node
        for node in nodes(plan)
        if node.get("Parent Relationship") == "InitPlan"
        and all(inner.get("Relation Name", "pg_").startswith("pg_") for inner in nodes(node))
    ]
    conditions = " ".join(str(node.get("Index Cond", "")) for node in every)
    return {
        "generic": "$" in conditions,
        "buffers": buffers(plan) - sum(buffers(gate) for gate in gates),
        "gate": sum(buffers(gate) for gate in gates),
        "rows": plan["Actual Rows"],
        "scanned": {node["Relation Name"] for node in every if node["Node Type"] == "Seq Scan"},
        "indexes": {node["Index Name"] for node in every if "Index Name" in node},
    }


def _one_part(session, raw, start, stop):
    """The window as it was before 20261116_0041 (the reference)."""
    from app.services.readers.measurement_windows import raw_interval_window

    return (raw_interval_window("steps", raw, start, stop),)


# The long part reads the long steps' index: a few pages, whatever the window or archive.
LONG_PART_PAGES = 8
WHOLE = {"raw_health_data", "steps"}


async def test_no_window_age_reads_more_than_the_old_window(pg_sessions):
    """Six years of steps beside a larger heart-rate archive, a few multi-day intervals: for a
    week ending today, a month, a year and five years ago, the step read never touches more
    buffers than the old one-part window (beyond the long steps' index pages and the catalog
    check, both fixed) and never scans the raw or step table whole, in a custom and in a
    generic plan."""
    import time

    begun = time.monotonic()
    end = date(2026, 11, 15)
    first = datetime(2020, 11, 1, tzinfo=UTC)
    async with pg_sessions.begin() as session:
        for part in YEARS.split(";"):
            await session.execute(
                text(part), {"first": first, "last": datetime(2026, 11, 16, tzinfo=UTC)}
            )
    await analyze(pg_sessions.kw["bind"])
    report = {}
    for mode in ("force_custom_plan", "force_generic_plan"):
        for age in (0, 30, 365, 5 * 365):
            day = end - timedelta(days=age)
            new = await _buffers(pg_sessions, day, mode)
            old = await _buffers(pg_sessions, day, mode, _one_part)
            assert new["rows"] == old["rows"], (mode, age)
            assert new["generic"] == old["generic"] == (mode == "force_generic_plan"), (mode, age)
            report[mode, age] = (
                new["buffers"],
                old["buffers"],
                sorted(new["scanned"] & WHOLE),
                sorted(old["scanned"] & WHOLE),
            )
    print(report, round(time.monotonic() - begun, 1))
    for key, (new_buffers, old_buffers, new_whole, old_whole) in report.items():
        assert not new_whole, (key, report)
        # Where the old window scanned a table whole (a generic plan here), the small
        # synthetic step table can cost fewer pages than probing a week's rows; that scan
        # grows with the archive, the probes do not. Otherwise never more pages than before.
        if not old_whole:
            assert new_buffers <= old_buffers + LONG_PART_PAGES, (key, report)
