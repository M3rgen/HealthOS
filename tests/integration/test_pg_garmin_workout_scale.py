"""Five times the workout archive must cost at most six times the PostgreSQL plan work."""

import json

import pytest
from garmin_workout_archive import START, STOP, grow_archive
from sqlalchemy import event, text

from app.services.providers.garmin_workout_values import workout_values

pytestmark = pytest.mark.integration


class StopQuery(Exception):
    """The EXPLAIN replaces execution rather than repeating the expensive read."""


def plan_work(node):
    return node.get("Actual Loops", 0) * sum(
        node.get(name, 0)
        for name in (
            "Actual Rows",
            "Rows Removed by Filter",
            "Rows Removed by Join Filter",
            "Rows Removed by Index Recheck",
        )
    ) + sum(plan_work(child) for child in node.get("Plans", ()))


async def test_workout_plan_scales_with_archive(pg_sessions, monkeypatch):
    work = {}
    for days in (1000, 5000):
        await grow_archive(pg_sessions, monkeypatch, 0 if days == 1000 else 1000, days)
        async with pg_sessions() as session:
            await session.execute(text("ANALYZE"))
            plans = []

            async def explain(statement, plans=plans, **kwargs):
                connection = await session.connection()

                def prefix(conn, cursor, sql, parameters, context, executemany):
                    return "EXPLAIN (ANALYZE, FORMAT JSON) " + sql, parameters

                event.listen(
                    connection.sync_connection, "before_cursor_execute", prefix, retval=True
                )
                try:
                    result = await connection.execute(
                        statement.execution_options(yield_per=None, stream_results=False)
                    )
                    plan = result.cursor.fetchone()[0]
                    plans.extend(json.loads(plan) if isinstance(plan, str) else plan)
                finally:
                    event.remove(connection.sync_connection, "before_cursor_execute", prefix)
                raise StopQuery

            monkeypatch.setattr(session, "stream", explain)
            with pytest.raises(StopQuery):
                _ = [value async for value in workout_values(session, START, STOP, "GARMIN")]
            assert plans[0]["Plan"]["Actual Rows"] == days
            work[days] = plan_work(plans[0]["Plan"])
            print(
                f"workout days={days} work={work[days]} ms={plans[0]['Execution Time']}", flush=True
            )
    assert work[5000] <= 6 * work[1000], work
