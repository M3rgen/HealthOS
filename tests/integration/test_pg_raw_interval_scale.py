"""A raw step/sleep window reads the rows near it; older history is not fetched and dropped.

``ix_raw_health_data_type_start`` bounds an interval only from above: before the added
``COALESCE(end, start) >= start`` predicate every older interval of the type was read and
filtered. The archive's history grows fivefold; the work for a recent window must not, and
the selected rows equal ``interval_window``'s, including boundaries, open ends and long
intervals that start long before the window.
"""

from datetime import UTC, datetime, timedelta

import pytest
from garmin_projection_archive import analyze, explain
from sqlalchemy import select, text

from app.models.state import RawHealthData
from app.services.readers.measurement_windows import interval_window, raw_interval_window

pytestmark = pytest.mark.integration
NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
START, STOP = NOW - timedelta(days=7), NOW
HOUR, DAY, QUARTER = timedelta(hours=1), timedelta(days=1), timedelta(minutes=15)
ROWS = """
INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, payload_hash,
                             first_seen_at, last_seen_at, source_start_time, source_end_time)
SELECT gen_random_uuid(), CAST(:prefix AS text) || extract(epoch FROM t)::bigint, :kind, '{}',
       'x', t, t, t, t + CAST(:length AS interval)
FROM generate_series(CAST(:first AS timestamptz), CAST(:last AS timestamptz),
                     CAST(:step AS interval)) t
"""
# (prefix, kind, first, last, step, length): edges of both windows and a long interval.
EDGES = [
    (
        "s-touch:",
        "steps",
        START - timedelta(hours=1),
        START - timedelta(hours=1),
        HOUR,
        HOUR,
    ),
    ("s-in:", "steps", START - timedelta(minutes=30), START, timedelta(minutes=30), HOUR),
    (
        "s-long:",
        "steps",
        START - timedelta(days=40),
        START - timedelta(days=40),
        DAY,
        timedelta(days=41),
    ),
    ("s-stop:", "steps", STOP, STOP, HOUR, HOUR),
    (
        "z-edge:",
        "sleep",
        START - timedelta(hours=8),
        START - timedelta(hours=7),
        HOUR,
        timedelta(hours=8),
    ),
    (
        "z-late:",
        "sleep",
        STOP - timedelta(hours=8),
        STOP - timedelta(hours=8),
        HOUR,
        timedelta(hours=8),
    ),
]

EXPECTED = {"steps": {"s-in", "s-long", "s-recent"}, "sleep": {"z-edge"}}


async def add(session, prefix, kind, first, last, step, length):
    await session.execute(
        text(ROWS),
        {
            "prefix": prefix,
            "kind": kind,
            "first": first,
            "last": last,
            "step": step,
            "length": length,
        },
    )


def ids(kind, window):
    return select(RawHealthData.google_data_point_id).where(
        RawHealthData.data_type == kind, *window
    )


async def test_raw_window_reads_only_rows_near_the_window(pg_sessions):
    plans, days = {}, 0
    async with pg_sessions.begin() as session:
        for edge in EDGES:
            await add(session, *edge)
        await add(session, "s-recent:", "steps", START, STOP, QUARTER, QUARTER)
        await session.execute(
            text(
                "INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, "
                "payload_hash, first_seen_at, last_seen_at, source_start_time) VALUES "
                "(gen_random_uuid(), 'open', 'steps', '{}', 'x', :t, :t, :t)"
            ),
            {"t": START - timedelta(days=1)},
        )
    for grown in (60, 300):
        async with pg_sessions.begin() as session:
            first = START - timedelta(days=grown)
            last = START - timedelta(days=days or 2)
            await add(session, f"s-old{grown}:", "steps", first, last, QUARTER, QUARTER)
            await add(session, f"z-old{grown}:", "sleep", first, last, DAY, timedelta(hours=8))
        days = grown
        await analyze(pg_sessions.kw["bind"])
        async with pg_sessions() as session:
            for kind in ("steps", "sleep"):
                exact = ids(
                    kind,
                    interval_window(
                        kind,
                        RawHealthData.source_start_time,
                        RawHealthData.source_end_time,
                        START,
                        STOP,
                    ),
                )
                bounded = ids(kind, raw_interval_window(kind, RawHealthData, START, STOP))
                found = set(await session.scalars(bounded))
                assert found == set(await session.scalars(exact))
                assert {key.split(":")[0] for key in found} == EXPECTED[kind]
            statement = ids("steps", raw_interval_window("steps", RawHealthData, START, STOP))
            plans[grown] = await explain(await session.connection(), statement)
    print(plans)
    assert plans[300]["work"] <= plans[60]["work"] + 10, plans
