"""Google heart bounds stream bounded payload batches and own only their result cursor."""

import asyncio
import gc
import tracemalloc
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, PHONE, snapshot, store, summary
from support.postgres_reads import socket_buffers
from test_google_heart_history import KIND, OLD

from app.services.interpretation import google_daily_readers as reader
from app.services.readers.reading import ArchiveBounds


@pytest.mark.parametrize("spread_days", [False, True])
async def test_bound_memory_and_queries_do_not_grow_with_archive(
    sessions, monkeypatch, spread_days
):
    monkeypatch.setattr(reader, "_SUMMARY_BATCH", 32)
    await store(sessions, KIND, summary(KIND, "phone", source=PHONE, beatsPerMinute=80))

    async def read(count):
        async with sessions() as session:
            stream = AsyncMock(wraps=session.stream)
            session.stream = stream
            bounds = await reader.archive_bounds(session, "heart", DAY, ZoneInfo("UTC"))
            assert stream.await_count == 1
            assert stream.call_args.kwargs["execution_options"] == {"yield_per": 32}
            assert bounds == ArchiveBounds(
                DAY - timedelta(days=count - 1) if spread_days else DAY, DAY
            )
            assert not session.identity_map and not session.new and not session.dirty

    peaks = []
    for start, count in ((0, 128), (128, 2048)):
        await store(
            sessions,
            KIND,
            *(
                summary(
                    KIND,
                    f"{i:05}",
                    DAY - timedelta(days=i if spread_days else 0),
                    beatsPerMinute=60,
                    padding="x" * 8192,
                )
                for i in range(start, count)
            ),
        )
        await read(count)  # warm up SQL compilation before tracing
        gc.collect()
        tracemalloc.start()
        try:
            await read(count)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    print(f"heart bounds (spread={spread_days}): 128={peaks[0]} B; 2048={peaks[1]} B")
    assert peaks[1] < peaks[0] + 256 * 1024 + socket_buffers(sessions)


@pytest.mark.parametrize("batch", [1, 2])
@pytest.mark.parametrize("outcome", ["empty", "complete", "error", "cancel", "consumer_error"])
async def test_bounds_release_cursor_preserving_session_and_archive(
    sessions, monkeypatch, outcome, batch
):
    if outcome != "empty":
        await store(
            sessions, KIND, *(summary(KIND, str(i), OLD, beatsPerMinute=60) for i in range(4))
        )
        await store(sessions, KIND, summary(KIND, "0", OLD, beatsPerMinute=65))
    monkeypatch.setattr(reader, "_SUMMARY_BATCH", batch)
    async with sessions.begin() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        for method in ("commit", "rollback", "close"):
            monkeypatch.setattr(session, method, AsyncMock(wraps=getattr(session, method)))
        opened, seen, paused = [], 0, asyncio.Event()
        stream, parse = session.stream, reader.payload_for
        failure = ValueError("synthetic heart bounds failure")

        async def tracked(*args, **kwargs):
            result = await stream(*args, **kwargs)
            opened.append(result)
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            next_row = type(result).__anext__

            async def interrupted_next(result):
                if outcome == "cancel" and seen == batch:
                    paused.set()
                    await asyncio.Event().wait()  # between fetches, outside SQLite I/O
                return await next_row(result)

            monkeypatch.setattr(type(result), "__anext__", interrupted_next)
            return result

        def interrupted_parse(*args):
            nonlocal seen
            if outcome == "error" and seen == batch:
                raise failure
            seen += 1
            return parse(*args)

        class BrokenMeasurement:
            @property
            def value(self):
                raise failure

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "payload_for", interrupted_parse)
        if outcome == "consumer_error":
            monkeypatch.setattr(reader, "DailyValue", lambda *args: BrokenMeasurement())
        task = asyncio.create_task(reader.archive_bounds(session, "heart", DAY, ZoneInfo("UTC")))
        try:
            if outcome == "cancel":
                await asyncio.wait_for(paused.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome in {"error", "consumer_error"}:
                with pytest.raises(ValueError) as caught:
                    await task
                assert caught.value is failure
            else:
                expected = (
                    ArchiveBounds(None, None) if outcome == "empty" else ArchiveBounds(OLD, OLD)
                )
                assert await task == expected
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(opened) == opened[0].close.await_count == 1 and opened[0].closed
        assert session.get_transaction() is transaction and transaction.is_active
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted
        assert not any(getattr(session, m).await_count for m in ("commit", "rollback", "close"))
