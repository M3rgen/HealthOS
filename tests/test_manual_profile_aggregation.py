"""Profile aggregates keep fixed memory while canonical reads retain every tied row."""

import gc
import tracemalloc
from datetime import timedelta
from decimal import Decimal

import pytest
from derived_records import T0, snapshot
from sqlalchemy import event, insert

from app.core.timeutil import as_utc
from app.models.manual import ManualMeasurement
from app.services.archive.manual_flags import record_flag
from app.services.dashboard_views.dashboard_profile import dashboard_profile
from app.services.readers import DashboardSources
from app.services.readers.dashboard_manual import latest_measurement_value

METRICS = ("height", "weight", "body_fat", "muscle")
STOP = T0 + timedelta(days=1)


@pytest.mark.parametrize("conflict", [False, True])
async def test_profile_memory_orm_loads_and_queries_are_independent_of_tied_count(
    sessions, conflict
):
    peaks, queries, loads = [], [], []
    for first, count in ((0, 128), (128, 4096)):
        async with sessions.begin() as session:
            await session.execute(
                insert(ManualMeasurement),
                [
                    dict(
                        metric=metric,
                        value=Decimal("80.0002" if conflict and i == 0 else "80.0001"),
                        measured_at=T0,
                        # A differing value comes last in the old evidence query's order.
                        created_at=T0 + timedelta(seconds=i),
                    )
                    for metric in METRICS
                    for i in range(first, count)
                ],
            )
        async with sessions.begin() as session:
            before = await snapshot(session)
            await dashboard_profile(session, STOP, DashboardSources())  # warm SQL caches
            statements, loaded = [], []

            def trace(conn, cursor, statement, parameters, context, many, recorded=statements):
                recorded.append(statement)

            def track_load(session, instance, recorded=loaded):
                recorded.append(type(instance))

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", trace)
            event.listen(session.sync_session, "loaded_as_persistent", track_load)
            transaction = session.get_transaction()
            gc.collect()
            tracemalloc.start()
            try:
                profile = await dashboard_profile(session, STOP, DashboardSources())
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
                event.remove(engine, "before_cursor_execute", trace)
                event.remove(session.sync_session, "loaded_as_persistent", track_load)
            queries.append(len(statements))
            loads.append(len(loaded))
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert session.get_transaction() is transaction and transaction.is_active
            assert not session.identity_map and not session.new and not session.dirty
            assert profile == {
                metric: dict(
                    metric=metric,
                    value=None if conflict else 80.0001,
                    measured_at=T0,
                    source="Manuel",
                    **({"uncertainties": ["conflicting_values"]} if conflict else {}),
                )
                for metric in METRICS
            }
            assert await snapshot(session) == before
    print(f"manual profile {conflict=}: {peaks=} bytes, {queries=}, ORM {loads=}")
    assert queries == [8, 8] and loads == [0, 0]
    assert peaks[1] < peaks[0] + 256 * 1024


@pytest.mark.parametrize("metric", METRICS)
async def test_empty_flagged_and_unflagged_groups_keep_caller_transaction(sessions, metric):
    async with sessions() as session:
        assert await latest_measurement_value(session, metric, STOP) is None
        assert await dashboard_profile(session, STOP, DashboardSources()) == {}
        row = ManualMeasurement(metric=metric, value=Decimal("80.0001"), measured_at=T0)
        session.add(row)
        await session.flush()
        await record_flag(session, row.id, "test", True)
        before = await snapshot(session)
        transaction = session.get_transaction()
        assert await latest_measurement_value(session, metric, STOP) is None
        assert await dashboard_profile(session, STOP, DashboardSources()) == {}
        assert await snapshot(session) == before
        assert session.get_transaction() is transaction and transaction.is_active
        await record_flag(session, row.id, "test", False)
        before = await snapshot(session)
        at, value = await latest_measurement_value(session, metric, STOP)
        assert as_utc(at) == T0
        assert isinstance(value, Decimal) and value == Decimal("80.0001")
        assert (await dashboard_profile(session, STOP, DashboardSources()))[metric][
            "value"
        ] == float(value)
        assert await snapshot(session) == before
        await session.rollback()  # the read must not commit the caller's pending writes
    async with sessions() as session:
        assert await dashboard_profile(session, STOP, DashboardSources()) == {}
