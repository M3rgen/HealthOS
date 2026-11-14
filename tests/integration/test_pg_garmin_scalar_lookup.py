"""Measure the actual writer SELECT: fixed-key I/O stays bounded as the table grows."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, text

from app.models.health import ScalarMetric
from app.services.providers.garmin_normalize_writing import write_rows

pytestmark = pytest.mark.integration


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


async def test_scalar_lookup_uses_both_index_columns_and_bounded_buffers(pg_sessions):
    engine = pg_sessions.kw["bind"].sync_engine
    statements = []

    def trace(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "scalar_metrics" in statement:
            statements.append((statement, parameters))

    measurements = []
    async with pg_sessions.begin() as session:
        for low, high in [(1, 200_000), (200_001, 800_000)]:
            await session.execute(
                text("""
                INSERT INTO scalar_metrics
                  (id, data_type, google_data_point_id, start_time, value,
                   attributes, created_at, updated_at)
                SELECT md5(i::text)::uuid, 'synthetic-type-' || (i % 16),
                       'synthetic-key-' || i, '2026-01-01'::timestamptz, i % 100,
                       json_build_object('synthetic', repeat('x', 200)), now(), now()
                FROM generate_series(CAST(:low AS integer), CAST(:high AS integer)) AS i
            """),
                {"low": low, "high": high},
            )
            await session.execute(text("ANALYZE scalar_metrics"))
            # Include both present and missing pairs; rollback only synthetic projections.
            savepoint = await session.begin_nested()
            event.listen(engine, "before_cursor_execute", trace)
            try:
                rows = [
                    {
                        "data_type": f"synthetic-type-{i % 16}",
                        "google_data_point_id": f"synthetic-key-{i}",
                        "provider": "GARMIN",
                        "value": i % 100,
                        "attributes": {},
                        "start_time": datetime(2026, 1, 1, tzinfo=UTC),
                    }
                    for i in (1, 2, 3, 900_001, 900_002, 900_003)
                ]
                assert await write_rows(session, ScalarMetric, rows) == (3, 0, 3)
            finally:
                event.remove(engine, "before_cursor_execute", trace)
            assert len(statements) == 1
            statement, parameters = statements.pop()
            await savepoint.rollback()
            connection = await session.connection()
            result = await connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
            )
            explained = result.scalar_one()[0]
            plan = explained["Plan"]
            scans = [node for node in nodes(plan) if "Index Cond" in node]
            assert scans, explained
            assert all(
                "data_type" in node["Index Cond"] and "google_data_point_id" in node["Index Cond"]
                for node in scans
            ), explained
            assert not any(node["Node Type"] == "Seq Scan" for node in nodes(plan)), explained
            # Root counters include children; summing every node double-counts accesses.
            blocks = plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]
            assert blocks > 0
            measurements.append(blocks)
            print(
                f"rows={high} node={plan['Node Type']} blocks={blocks} "
                f"read={plan['Shared Read Blocks']} ms={explained['Execution Time']}"
            )
    assert measurements[1] <= measurements[0] * 2, measurements
