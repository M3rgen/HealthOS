"""Manual coverage keeps record counts and civil days without buffering the archive."""

import asyncio
import gc
import tracemalloc
import uuid
from contextlib import aclosing
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import snapshot
from sqlalchemy import event, insert
from support.clock import freeze_app_clock

from app.models.manual import ManualMeasurement
from app.models.manual_flags import ManualMeasurementFlagEvent
from app.services.synchronization import coverage
from app.services.synchronization import coverage_reading as reader
from app.services.synchronization.coverage_types import Window

METRICS = tuple(metric for metric in reader.METRICS if metric.manual)
DAY = date(2026, 3, 29)
ZONE = ZoneInfo("Europe/Brussels")


@pytest.mark.parametrize("metric", METRICS, ids=lambda m: m.key)
@pytest.mark.parametrize("day,hours", [(DAY, 23), (date(2026, 10, 25), 25)])
async def test_manual_flags_civil_boundaries_and_today(sessions, monkeypatch, metric, day, hours):
    window = Window(day, day + timedelta(days=1), day + timedelta(days=1), ZONE)
    tomorrow = datetime.combine(window.today, datetime.min.time(), ZONE)
    freeze_app_clock(monkeypatch, tomorrow + timedelta(hours=12))
    assert (tomorrow.astimezone(window.start.tzinfo) - window.start).total_seconds() == hours * 3600
    times = [
        window.start - timedelta(microseconds=1),
        window.start,
        tomorrow - timedelta(microseconds=1),
        tomorrow,
        window.stop,
        window.start + timedelta(hours=1),
        tomorrow + timedelta(hours=1),
    ]
    rows = [
        ManualMeasurement(metric=metric.manual, value=0, measured_at=at.astimezone(UTC))
        for at in times
    ]
    async with sessions.begin() as session:
        session.add_all(rows)
        await session.flush()
        # Insert out of id order: ties use id, later timestamps beat even higher ids.
        for index, actions in [
            (0, ["mark"]),
            (1, ["mark", "unmark"]),
            (2, ["unmark", "mark"]),
            (5, ["mark"]),
            (6, ["mark"]),
        ]:
            for step in reversed(range(len(actions))):
                session.add(
                    ManualMeasurementFlagEvent(
                        id=100 * (index + 1) + step,
                        measurement_id=rows[index].id,
                        flag="test",
                        action=actions[step],
                        created_at=window.stop,
                    )
                )
        session.add(
            ManualMeasurementFlagEvent(
                id=9999,
                measurement_id=rows[1].id,
                flag="test",
                action="mark",
                created_at=window.start,
            )
        )
    async with sessions() as session:
        before = await snapshot(session)
        card = await coverage.coverage_card(session, day, window.end, ZONE.key, metric.key)
        result = card.metrics[0]
        assert (result.value_days, result.days_without_value, result.partial_today) == (1, 0, True)
        assert result.sampling.sample_count == 2 and result.exclusions.manually_excluded == 3
        [regime] = result.source_regimes
        assert (regime.source, regime.origin, regime.first_date, regime.last_date) == (
            "manuel",
            "manual",
            day,
            window.today,
        )
        assert (regime.value_days, regime.partial_today, regime.record_count) == (1, True, 2)
        for first, end, excluded in [
            (day + timedelta(days=3), day + timedelta(days=3), 0),
            (day, day, 3),
        ]:
            # The first day has values initially; append a later mark to exclude its last row.
            if excluded:
                session.add(
                    ManualMeasurementFlagEvent(
                        measurement_id=rows[1].id,
                        flag="test",
                        action="mark",
                        created_at=window.stop + timedelta(seconds=1),
                    )
                )
                await session.flush()
                before = await snapshot(session)
            empty = (
                await coverage.coverage_card(session, first, end, ZONE.key, metric.key)
            ).metrics[0]
            assert empty.value_days == empty.sampling.sample_count == 0
            assert empty.source_regimes == [] and not empty.partial_today
            assert empty.exclusions.manually_excluded == excluded
            assert await snapshot(session) == before


@pytest.mark.parametrize("metric", METRICS, ids=lambda m: m.key)
@pytest.mark.parametrize("outside", [False, True])
async def test_manual_coverage_peak_memory_and_queries_stay_bounded(sessions, metric, outside):
    window = Window(DAY, DAY, DAY + timedelta(days=1), ZONE)
    peaks, queries = [], []
    # Both sizes fill a production batch; only archive size changes.
    for first, count in ((0, 2048), (2048, 8192)):
        # Bulk preparation, including flags, is outside the traced read.
        values = [
            dict(
                id=uuid.uuid4(),
                metric=metric.manual,
                value=71,
                measured_at=window.start - timedelta(days=10) if outside else window.start,
            )
            for _ in range(first, count)
        ]
        async with sessions.begin() as session:
            await session.execute(insert(ManualMeasurement), values)
            await session.execute(
                insert(ManualMeasurementFlagEvent),
                [dict(measurement_id=row["id"], flag="test", action="mark") for row in values[::2]]
                if not outside
                else [dict(measurement_id=row["id"], flag="test", action="mark") for row in values],
            )
        async with sessions() as session:
            await coverage.coverage_card(session, DAY, DAY, ZONE.key, metric.key)
            statements = []

            def trace(conn, cursor, statement, parameters, context, many, recorded=statements):
                recorded.append(statement)

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", trace)
            gc.collect()
            tracemalloc.start()
            try:
                result = (
                    await coverage.coverage_card(session, DAY, DAY, ZONE.key, metric.key)
                ).metrics[0]
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
                event.remove(engine, "before_cursor_execute", trace)
            queries.append(len(statements))
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert not session.identity_map and not session.new and not session.dirty
            assert result.value_days == int(not outside)
            assert result.sampling.sample_count == (0 if outside else count // 2)
            assert result.exclusions.manually_excluded == (0 if outside else count // 2)
    print(f"manual coverage {metric.key} {outside=}: {peaks=} bytes, {queries=}")
    assert queries[0] == queries[1] and queries[1] <= 5
    assert peaks[1] < peaks[0] + 256 * 1024


@pytest.mark.parametrize("failure", [None, "read", "convert", "cancel", "early"])
async def test_manual_cursor_closes_on_completion_failure_and_cancellation(
    sessions, monkeypatch, failure
):
    window = Window(DAY, DAY, DAY + timedelta(days=1), ZONE)
    async with sessions() as session:
        session.add_all(
            [
                ManualMeasurement(metric="muscle", value=40, measured_at=window.start)
                for _ in range(3)
            ]
        )
        await session.flush()
        before = await snapshot(session)
        opened, stream, seen = [], session.stream, 0

        async def tracked(statement, **kwargs):
            assert kwargs["execution_options"]["yield_per"] == 1
            assert len(statement.selected_columns) == 2
            assert statement.selected_columns[0].name == "measured_at"
            result = await stream(statement, **kwargs)
            opened.append(result)
            next_row = type(result).__anext__

            async def next_checked(result):
                nonlocal seen
                seen += 1
                if seen == 2 and failure == "read":
                    raise ValueError("synthetic fetch failure")
                if seen == 2 and failure == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                row = await next_row(result)
                assert isinstance(row[1], bool)
                return row

            monkeypatch.setattr(type(result), "__anext__", next_checked)
            return result

        def bad_local(at):
            raise ValueError("synthetic conversion failure")

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "_MANUAL_BATCH", 1)
        if failure == "convert":
            monkeypatch.setattr(window, "local", bad_local)
        if failure == "early":
            async with aclosing(reader._manual_records(session, METRICS[-1], window)) as rows:
                async for _ in rows:
                    break
        elif failure is None:
            assert (await reader.manual_stream(session, METRICS[-1], window))[0].records == 3
        else:
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                await reader.manual_stream(session, METRICS[-1], window)
        assert len(opened) == 1 and opened[0].closed
        assert await snapshot(session) == before
