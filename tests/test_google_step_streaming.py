"""Production-ingested step streams release only their cursor, including on cancellation."""

import asyncio
import gc
import tracemalloc
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, T0, snapshot, steps, store
from support.postgres_reads import is_plan_control, rows_in_flight, socket_buffers

from app.services.interpretation import google_daily_readers as reader
from app.services.readers import CalendarRead, DashboardSources, daily_window
from app.services.readers.reading import ArchiveBounds


@pytest.mark.parametrize("batch", [1, 2])
@pytest.mark.parametrize("outcome", ["empty", "complete", "error", "cancel"])
@pytest.mark.parametrize("bounds", [False, True])
async def test_step_cursor_closes_preserving_transaction(
    sessions, monkeypatch, batch, outcome, bounds
):
    if outcome != "empty":
        await store(sessions, "steps", *(steps(str(i), count=1) for i in range(5)))
        await store(sessions, "steps", steps("0", count=2))  # preserve correction history too
    monkeypatch.setattr(reader, "_STEP_BATCH", batch)
    zone = ZoneInfo("UTC")
    calendar = CalendarRead(end_date=DAY, zone=zone, sources=DashboardSources())
    async with sessions.begin() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        for method in ("commit", "rollback", "close"):
            monkeypatch.setattr(session, method, AsyncMock(wraps=getattr(session, method)))
        opened, seen, reads = [], 0, {}
        parser = "step_window"
        stream, parse = session.stream_scalars, getattr(reader, parser)
        paused = asyncio.Event()
        failure = ValueError("synthetic step processing failure")

        async def tracked(*args, **kwargs):
            assert kwargs["execution_options"]["yield_per"] == batch
            result = await stream(*args, **kwargs)
            opened.append(result)
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            next_row = type(result).__anext__

            async def interrupted_next(result):
                if outcome == "cancel" and seen == batch:
                    paused.set()
                    await asyncio.Event().wait()  # pause between batches, outside SQLite I/O
                return await next_row(result)

            monkeypatch.setattr(type(result), "__anext__", interrupted_next)
            return result

        def interrupted_parse(*args):
            nonlocal seen
            if outcome == "error" and seen == batch:
                raise failure
            seen += 1
            return parse(*args)

        monkeypatch.setattr(session, "stream_scalars", tracked)
        monkeypatch.setattr(reader, parser, interrupted_parse)
        task = asyncio.create_task(
            reader.archive_bounds(session, "steps", DAY, zone)
            if bounds
            else reader.read_steps(session, daily_window(DAY, 1, zone), reads, calendar=calendar)
        )
        try:
            if outcome == "cancel":
                await asyncio.wait_for(paused.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "error":
                with pytest.raises(ValueError) as caught:
                    await task
                assert caught.value is failure and seen == batch
            else:
                result = await task
                if bounds:
                    assert result == (
                        ArchiveBounds(None, None) if outcome == "empty" else ArchiveBounds(DAY, DAY)
                    )
                elif outcome == "empty":
                    assert reads == {}
                else:
                    assert reads["FITBIT"].days[DAY]["steps"].value == 6
                    assert reads["FITBIT"].step_hours == [6.0] + [None] * 23
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(opened) == opened[0].close.await_count == 1 and opened[0].closed
        assert session.get_transaction() is transaction and transaction.is_active
        assert await snapshot(session) == before  # real queries prove the session still works
        assert not session.new and not session.dirty and not session.deleted
        assert not any(getattr(session, m).await_count for m in ("commit", "rollback", "close"))


@pytest.mark.parametrize("spread_days", [False, True])
async def test_step_bounds_memory_stays_batched(sessions, monkeypatch, spread_days):
    monkeypatch.setattr(reader, "_STEP_BATCH", 32)

    async def read(count):
        async with sessions() as session:
            session.stream_scalars = AsyncMock(wraps=session.stream_scalars)
            result = await reader.archive_bounds(session, "steps", DAY, ZoneInfo("UTC"))
            assert result == ArchiveBounds(
                DAY - timedelta(days=count - 1) if spread_days else DAY, DAY
            )
            assert session.stream_scalars.await_count == 1
            assert session.stream_scalars.call_args.kwargs["execution_options"] == {"yield_per": 32}
            assert not session.identity_map and not session.new and not session.dirty

    peaks = []
    for start, count in ((0, 128), (128, 2048)):
        records = []
        for i in range(start, count):
            record = steps(str(i), start=T0 - timedelta(days=i if spread_days else 0))
            record["padding"] = "x" * 8192
            records.append(record)
        await store(sessions, "steps", *records)
        del records, record
        await read(count)  # warm SQL compilation before measuring only the reader
        gc.collect()
        tracemalloc.start()
        try:
            await read(count)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    print(f"step bounds (spread={spread_days}): 128={peaks[0]} B; 2048={peaks[1]} B")
    assert peaks[1] < peaks[0] + 256 * 1024 + socket_buffers(sessions)


async def test_long_dense_bounds_release_days_and_keep_one_query(sessions, monkeypatch):
    from sqlalchemy import event

    from app.services.interpretation import step_totals

    monkeypatch.setattr(reader, "_STEP_BATCH", 16)
    original = step_totals.StepTotals.add
    maxima = {"days": 0, "tails": 0}

    def tracked_add(self, *args):
        original(self, *args)
        maxima["days"] = max(maxima["days"], len(self.days))
        assert len(self.days) <= 3

    advance = step_totals.StepSweep.advance

    def tracked_advance(self, before):
        yield from advance(self, before)
        maxima["tails"] = max(maxima["tails"], len(self.pending))

    monkeypatch.setattr(step_totals.StepTotals, "add", tracked_add)
    monkeypatch.setattr(step_totals.StepSweep, "advance", tracked_advance)
    peaks = []
    for first, days, density in ((0, 8, 8), (8, 96, 8), (0, 96, 24)):
        await store(
            sessions,
            "steps",
            *(
                steps(
                    f"{day}-{i}",
                    1,
                    T0 - timedelta(days=day, hours=25),
                    T0 - timedelta(days=day, hours=1),
                )
                | {"padding": "x" * 8192}
                for day in range(first, days)
                for i in range(8 if density == 24 else 0, density)
            ),
        )
        await store(
            sessions,
            "steps",
            steps("spanning", 1, T0 - timedelta(days=days + 1), T0 - timedelta(hours=1)),
        )
        async with sessions() as session:
            await reader.archive_bounds(session, "steps", DAY, ZoneInfo("UTC"))
            gc.collect()
            statements = []

            def trace(conn, cursor, statement, parameters, context, many, *, captured=statements):
                if is_plan_control(statement):
                    return
                assert statement.lstrip().startswith("SELECT")
                captured.append(statement)

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", trace)
            tracemalloc.start()
            try:
                result = await reader.archive_bounds(session, "steps", DAY, ZoneInfo("UTC"))
                peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
                event.remove(engine, "before_cursor_execute", trace)
            assert result == ArchiveBounds(DAY - timedelta(days=days + 1), DAY - timedelta(days=1))
            assert len(statements) == 1
            assert not session.identity_map and not session.new and not session.dirty
    assert maxima["days"] <= 3 and maxima["tails"] <= 2 * 24
    print(f"long dense step bounds peaks: {peaks}")
    # The 8-day archive ends inside asyncpg's first prefetch; later reads also hold its steady
    # state: the rows it prefetches beyond two batches (8 KiB padding each) and socket buffers.
    driver = (rows_in_flight(sessions, 16) - 2 * 16) * 9 * 1024 + socket_buffers(sessions)
    assert peaks[1] < peaks[0] + 256 * 1024 + driver
    assert max(peaks) < 2 * 1024 * 1024 + socket_buffers(sessions)


async def test_step_bounds_closes_cursor_on_yield_consumer_failure(sessions, monkeypatch):
    # Fail after the sweep yields its first completed day while the SQL cursor
    # still has more batches. aclosing must release this suspended generator too.
    await store(sessions, "steps", *(steps(str(i), start=T0 - timedelta(days=i)) for i in range(8)))
    monkeypatch.setattr(reader, "_STEP_BATCH", 1)
    failure = ValueError("synthetic consumer failure")

    class BrokenMeasurement:
        @property
        def value(self):
            raise failure

    monkeypatch.setattr(reader, "DailyValue", lambda *args: BrokenMeasurement())
    async with sessions.begin() as session:
        before = await snapshot(session)
        original, opened = session.stream_scalars, []

        async def stream(*args, **kwargs):
            result = await original(*args, **kwargs)
            opened.append(result)
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            return result

        monkeypatch.setattr(session, "stream_scalars", stream)
        with pytest.raises(ValueError) as caught:
            await reader.archive_bounds(session, "steps", DAY, ZoneInfo("UTC"))
        assert caught.value is failure
        assert len(opened) == opened[0].close.await_count == 1 and opened[0].closed
        assert session.in_transaction() and await snapshot(session) == before
