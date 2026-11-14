"""Both heart reads must scale with distinct sources, in legacy and mixed archives."""

import pytest
from garmin_heart_archive import START, STOP, grow_archive
from garmin_projection_archive import analyze, explain

from app.services.providers.garmin_heart_values import heart_values

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("all_legacy", [True, False], ids=["legacy", "mixed"])
async def test_both_read_plans_grow_at_most_six_fold(
    pg_sessions, monkeypatch, all_legacy, record_property
):
    plans, grown = {}, 0
    for sources in (1000, 5000):
        await grow_archive(pg_sessions, grown, sources, all_legacy=all_legacy)
        grown = sources
        await analyze(pg_sessions.kw["bind"])
        async with pg_sessions() as session:
            queries, stream = [], session.stream

            async def capture(statement, *, queries=queries, stream=stream, **kwargs):
                queries.append(statement.execution_options(yield_per=None, stream_results=False))
                return await stream(statement, **kwargs)

            monkeypatch.setattr(session, "stream", capture)
            assert await heart_values(session, START, STOP, "GARMIN")
            assert len(queries) == 2
            connection = await session.connection()
            plans[sources] = [await explain(connection, statement) for statement in queries]
            eligible = sources if all_legacy else sum(i % 12 not in (2, 6) for i in range(sources))
            inputs = (
                sources
                if all_legacy
                else sum(i % 12 in (0, 1, 3, 4, 7, 8, 11) for i in range(sources))
            )
            assert [plan["rows"] for plan in plans[sources]] == [3 * eligible, inputs]
    record_property("postgres_plans", plans)
    print("legacy" if all_legacy else "mixed", plans)
    # Check each stream as well as their total: normalized work cannot mask raw rescans.
    for index in (0, 1):
        assert plans[5000][index]["work"] <= 6 * plans[1000][index]["work"], plans
    assert sum(p["work"] for p in plans[5000]) <= 6 * sum(p["work"] for p in plans[1000]), plans
