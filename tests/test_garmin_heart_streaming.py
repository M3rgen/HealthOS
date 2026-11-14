"""Daily source evidence is streamed once, with bounded JSON buffering."""

import asyncio
import gc
import tracemalloc
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from garmin_records import heart_rate, store_page
from sqlalchemy import event
from test_garmin_heart_validity import START, STOP, snapshot
from test_garmin_normalize import DAY, NOW, garmin_heart
from test_garmin_step_validity import only_select

from app.services.providers import garmin_heart_values
from app.services.providers.garmin_normalize import normalize_window
from app.services.providers.garmin_readers import heart_rate_samples


async def test_query_count_and_raw_buffer_stay_bounded_across_days_and_samples(
    sessions, monkeypatch
):
    monkeypatch.setattr(garmin_heart_values, "_SAMPLE_BATCH", 32)
    monkeypatch.setattr(garmin_heart_values, "_RAW_BATCH", 2)
    for days, per_day in ((12, 2), (12, 24), (96, 24)):
        expected = []
        for offset in range(days):
            day = DAY + timedelta(days=offset)
            body = heart_rate(day)
            samples = []
            for minute in range(per_day):
                moment = START + timedelta(days=offset, minutes=minute)
                # Extra sample fields are legal; large arrays reveal eager buffering.
                value = "-1e-10000" if minute % 3 == 0 else 60 + minute
                samples.append([int(moment.timestamp() * 1000), value, "x" * 8192])
                if minute % 3:
                    expected.append((moment, float(value)))
            body["heartRateValues"] = samples
            body["syntheticUnrelatedField"] = "x" * 32768
            await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=per_day))
        last = DAY + timedelta(days=days - 1)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, last)
        del body, samples
        async with sessions() as session:
            stop = START + timedelta(days=days)
            await heart_rate_samples(session, START, stop)  # warm SQL caches outside measurement
            before = await snapshot(session)
            queries, opened = [], []
            stream = session.stream

            async def tracked(query, *, _queries=queries, _stream=stream, _opened=opened, **kwargs):
                _queries.append(query)
                result = await _stream(query, **kwargs)
                _opened.append(result)
                return result

            with monkeypatch.context() as patch:
                patch.setattr(session, "stream", tracked)
                gc.collect()
                tracemalloc.start()
                try:
                    result = await heart_rate_samples(session, START, stop)
                    retained, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            assert result == expected
            assert len(queries) == 2  # independent of days and sample count
            assert [query.get_execution_options()["yield_per"] for query in queries] == [32, 2]
            assert len(queries[1].selected_columns) == 2  # key and JSON-member text only
            assert all(result.closed for result in opened)
            # O(samples) return values/dictionary are allowed; archived arrays are not.
            assert peak - retained < 2 * 1024 * 1024
            assert await snapshot(session) == before
            assert not session.identity_map and not session.new and not session.dirty
            print(f"Garmin {days=} {per_day=}: {retained=} B, {peak=} B, queries=2")


@pytest.mark.parametrize("failure", [None, "parse", "fetch", "cancel"])
async def test_raw_stream_closes_on_completion_failure_and_cancellation(
    sessions, monkeypatch, failure
):
    for offset in range(3):
        day = DAY + timedelta(days=offset)
        # The archive key may differ from sample dates; the reader must not filter it by day.
        await store_page(sessions, "heart-rate", [garmin_heart(day, [(offset, "-1e-10000")])], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY + timedelta(days=2))
    monkeypatch.setattr(garmin_heart_values, "_RAW_BATCH", 1)
    async with sessions.begin() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        opened, parsed = [], 0
        stream, parse = session.stream, garmin_heart_values._negative_keys

        async def tracked(*args, **kwargs):
            result = await stream(*args, **kwargs)
            opened.append(result)
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            return result

        def interrupted_parse(key, samples):
            nonlocal parsed
            parsed += 1
            if parsed == 2 and failure == "parse":
                raise ValueError("synthetic parse failure")
            return parse(key, samples)

        next_row = None

        async def interrupted_next(result):
            if parsed == 2:
                if failure == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
                if failure == "fetch":
                    raise ValueError("synthetic fetch failure")
            return await next_row(result)

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(garmin_heart_values, "_negative_keys", interrupted_parse)
        # AsyncResult.tuples() iterates the same result, including the driver fetch path.
        from sqlalchemy.ext.asyncio import AsyncResult

        next_row = AsyncResult.__anext__
        monkeypatch.setattr(AsyncResult, "__anext__", interrupted_next)
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            if failure is None:
                assert await heart_rate_samples(session, START, STOP) == []
            else:
                with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                    await heart_rate_samples(session, START, STOP)
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
        assert len(opened) == 2 and all(result.closed for result in opened)
        for result in opened:
            result.close.assert_awaited_once()
        assert session.get_transaction() is transaction
        assert await snapshot(session) == before
