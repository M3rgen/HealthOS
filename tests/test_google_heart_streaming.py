"""Real Google ingestion bounds raw buffering while retaining every small heart sample."""

import asyncio
import gc
import tracemalloc
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from derived_records import BAND, PHONE, T0, snapshot, store
from sqlalchemy import event
from support.postgres_reads import rows_in_flight, socket_buffers
from test_heart_corrections import heart

from app.models.health import HeartRate
from app.services.readers import DashboardSources, current_heart


async def read(session):
    return await current_heart.heart_rate_samples(
        session, T0, T0 + timedelta(days=1), DashboardSources().normalized(HeartRate, "heart-rate")
    )


@pytest.mark.parametrize("payload_bytes", [0, 8192])
async def test_peak_memory_is_batch_plus_returned_samples(sessions, monkeypatch, payload_bytes):
    monkeypatch.setattr(current_heart, "_HEART_BATCH", 16)
    for first, count in ((0, 128), (128, 2048)):
        await store(
            sessions,
            "heart-rate",
            *[
                {
                    **heart(str(i), T0 + timedelta(seconds=i), beatsPerMinute=i),
                    "syntheticPadding": "x" * payload_bytes,
                }
                for i in range(first, count)
            ],
        )
        async with sessions() as session:
            await read(session)  # ingestion and SQL warmup are outside the measurement
            gc.collect()
            # A collection inside the window would free the query's own reference cycles and
            # count them as transient; decoded JSON is freed by reference counting regardless.
            gc.disable()
            tracemalloc.start()
            try:
                result = await read(session)
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
                gc.enable()
            assert result == [(T0 + timedelta(seconds=i), i, "HEALTH_KIT") for i in range(count)]
            assert not session.identity_map and not session.new and not session.dirty
        print(f"heart {count=} {payload_bytes=}: {retained=} B, {peak=} B")
        # Retained memory includes the O(count) output; only temporary JSON is bounded.
        assert retained < 1536 * 1024 + count * 256  # query overhead plus small sample tuples
        transient = rows_in_flight(sessions, 16) * (payload_bytes + 2048) + socket_buffers(sessions)
        assert peak - retained < transient + 256 * 1024


@pytest.mark.parametrize("failure", [None, "parse", "fetch", "cancel"])
async def test_stream_closes_and_caller_keeps_transaction(sessions, monkeypatch, failure):
    await store(sessions, "heart-rate", *(heart(str(i), beatsPerMinute=i) for i in range(7)))
    monkeypatch.setattr(current_heart, "_HEART_BATCH", 1)
    async with sessions.begin() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        opened, seen = [], 0
        stream, parse = session.stream_scalars, current_heart.current_heart_rate

        async def tracked(*args, **kwargs):
            assert args[0].get_execution_options()["yield_per"] == 1
            result = await stream(*args, **kwargs)
            opened.append(result)
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            next_row = type(result).__anext__

            async def interrupted_next(result):
                if seen == 2 and failure == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)  # real cancellation before the next driver fetch
                if seen == 2 and failure == "fetch":
                    raise ValueError("synthetic fetch failure")
                return await next_row(result)

            monkeypatch.setattr(type(result), "__anext__", interrupted_next)
            return result

        def interrupted_parse(record):
            nonlocal seen
            seen += 1
            if seen == 2 and failure == "parse":
                raise ValueError("synthetic parse failure")
            return parse(record)

        statements = []
        engine = session.get_bind()

        def trace(conn, cursor, statement, parameters, context, many):
            statements.append(statement)

        monkeypatch.setattr(session, "stream_scalars", tracked)
        monkeypatch.setattr(current_heart, "current_heart_rate", interrupted_parse)
        event.listen(engine, "before_cursor_execute", trace)
        try:
            if failure is None:
                assert len(await read(session)) == 7
            else:
                with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                    await read(session)
        finally:
            event.remove(engine, "before_cursor_execute", trace)
        assert len(opened) == len(statements) == 1
        opened[0].close.assert_awaited_once()
        assert opened[0].closed and session.get_transaction() is transaction
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted


async def test_batch_boundaries_preserve_order_zero_and_field_priority(sessions, monkeypatch):
    monkeypatch.setattr(current_heart, "_HEART_BATCH", 3)
    records = [
        heart(
            str(i),
            T0 + timedelta(seconds=i),
            BAND if i % 2 else PHONE,
            beatsPerMinute=0 if i % 3 == 0 else None,
            bpm=i,
            value=999,
        )
        for i in range(137)
    ]
    await store(sessions, "heart-rate", *reversed(records))
    # Real corrections leave the old normalized values behind, including a moved time.
    await store(
        sessions,
        "heart-rate",
        heart("3", T0 + timedelta(days=1), bpm="invalid"),
        heart("6", T0 + timedelta(seconds=6), beatsPerMinute="NaN", bpm=66),
        heart("9", T0 + timedelta(seconds=9), beatsPerMinute=-60, bpm=99),
        heart("12", T0 + timedelta(seconds=12), beatsPerMinute="-1e-1000", bpm=99),
    )
    async with sessions() as session:
        before = await snapshot(session)
        assert await read(session) == [
            (T0 + timedelta(seconds=i), 0 if i % 3 == 0 else i, "FITBIT" if i % 2 else "HEALTH_KIT")
            for i in range(137)
            if i not in (3, 6, 9, 12)
        ]
        assert await snapshot(session) == before
