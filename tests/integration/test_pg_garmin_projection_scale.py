"""Fivefold Garmin archives must use at most sixfold plan work, not quadratic joins."""

from datetime import date

import pytest
from garmin_projection_archive import SEEN, analyze, explain, grow_archive
from test_garmin_projection_values import METRICS, query

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("all_legacy", (False, True), ids=("mixed", "legacy"))
async def test_plan_work_grows_at_most_six_fold(pg_sessions, metric, all_legacy, record_property):
    engine, plans, grown = pg_sessions.kw["bind"], {}, 0
    for days in (1000, 5000):
        await grow_archive(pg_sessions, metric, grown, days, all_legacy=all_legacy)
        grown = days
        await analyze(engine)
        async with engine.connect() as connection:
            for reference in (True, False):
                plan = await explain(
                    connection, await query(metric, date.min, SEEN.date(), reference)
                )
                assert plan["rows"] == (days if all_legacy else days * 9 // 10)
                plans[f"{'old' if reference else 'new'}:{days}"] = plan
    record_property("postgres_plans", plans)
    print(metric, "legacy" if all_legacy else "mixed", plans)
    assert plans["old:5000"]["work"] > 6 * plans["old:1000"]["work"], plans
    assert plans["new:5000"]["work"] <= 6 * plans["new:1000"]["work"], plans
