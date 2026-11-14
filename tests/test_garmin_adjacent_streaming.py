"""Synthetic production archives keep adjacent-day reads bounded and release cursors."""

import asyncio
import weakref
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from garmin_records import heart_rate, store_page
from sqlalchemy import event
from test_garmin_normalize import DAY, NOW, raw
from test_garmin_step_validity import snapshot

from app.models.garmin import GarminRawRecord
from app.services.providers import garmin_time as reader


@pytest.mark.parametrize("gap", [8, 64])
@pytest.mark.parametrize("batch", [1, 4, 16])
async def test_sparse_archive_uses_two_bounded_selects(sessions, monkeypatch, gap, batch):
    monkeypatch.setattr(reader, "_ADJACENT_BATCH", batch)
    end = DAY + timedelta(days=2)
    expected, candidates = {}, [heart_rate(DAY + timedelta(days=i)) for i in range(3)]
    for sign in (-1, 1):
        boundary = DAY if sign < 0 else end
        for distance in (*range(1, gap + 1), 80, 90):
            day = boundary + timedelta(days=sign * distance)
            body = heart_rate(day)
            if distance <= gap:
                body = {"calendarDate": str(day), "unread": "x" * 8192}
            elif distance == 80:
                expected[str(day)] = body
            candidates.append(body)
    for body in reversed(candidates):
        await store_page(sessions, "heart-rate", [body], NOW)
    async with sessions.begin() as session:
        # Include malformed dates and unobserved versions alongside real sightings.
        for sign in (-1, 1):
            boundary = DAY if sign < 0 else end
            day = boundary + timedelta(days=sign * 2)
            key = str(day) + "x"  # malformed day keys must also be skipped
            session.add(raw("heart-rate", key, heart_rate(day)))
            legacy = str(boundary + timedelta(days=sign))
            session.add(
                raw("heart-rate", legacy, heart_rate(date.fromisoformat(legacy)), suffix="b")
            )
            legacy = str(boundary + timedelta(days=sign * (gap + 2)))
            session.add(raw("heart-rate", legacy, heart_rate(date.fromisoformat(legacy))))
            session.add(raw("heart-rate", legacy, [], suffix="b"))
    live, peaks, statements, seen = weakref.WeakValueDictionary(), [], [], []

    class Body(dict):
        pass

    def track_body(body):
        if isinstance(body, dict) and "calendarDate" in body:
            body = Body(body)
            live[id(body)] = body
            peaks.append(len(live))
        return body

    def capture(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().startswith("SELECT")
        assert context.execution_options["yield_per"] == batch
        statements.append(statement)

    parse = reader.heart_zone

    def tracked(day, body):
        seen.append(day)
        return parse(day, body)

    engine = sessions.kw["bind"].sync_engine
    body_type = GarminRawRecord.body.type
    processor = body_type._cached_result_processor

    def tracked_processor(dialect, coltype):
        decode = processor(dialect, coltype)
        return lambda value: track_body(decode(value) if decode else value)

    monkeypatch.setattr(body_type, "_cached_result_processor", tracked_processor)
    monkeypatch.setattr(reader, "heart_zone", tracked)
    async with sessions() as session:
        event.listen(engine, "before_cursor_execute", capture)
        try:
            found = await reader.adjacent_hearts(session, DAY, end)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert found == expected and not session.identity_map
    assert len(statements) == 2 and "DESC" in statements[0]
    assert all(min(abs((day - DAY).days), abs((day - end).days)) <= 80 for day in seen)
    assert len(seen) == 2 * (gap + 2)  # only current versions, no farther evidence
    # SQLAlchemy briefly overlaps old/new batches; only the winning bodies remain.
    assert max(peaks) <= 2 * batch + 2 and len(live) == 2


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("outcome", ["early", "invalid", "empty", "parse", "fetch", "cancel"])
async def test_results_close_without_ending_caller_transaction(
    sessions, monkeypatch, side, outcome
):
    monkeypatch.setattr(reader, "_ADJACENT_BATCH", 1)
    if outcome != "empty":
        bodies = [heart_rate(DAY + timedelta(days=side * i)) for i in range(1, 6)]
        for body in bodies:
            if outcome == "invalid":
                body = {"calendarDate": body["calendarDate"]}
            await store_page(sessions, "heart-rate", [body], NOW)
    async with sessions.begin() as session:
        before, transaction = await snapshot(session), session.get_transaction()
        opened, seen = [], 0
        stream, parse = session.stream, reader.heart_zone
        failure, paused = ValueError("synthetic adjacent failure"), asyncio.Event()
        for method in ("commit", "rollback", "close"):
            monkeypatch.setattr(session, method, AsyncMock(wraps=getattr(session, method)))
        next_row = None

        async def tracked(*args, **kwargs):
            nonlocal next_row
            rows = await stream(*args, **kwargs)
            opened.append(rows)
            monkeypatch.setattr(rows, "close", AsyncMock(wraps=rows.close))
            next_row = next_row or type(rows).__anext__

            async def interrupted_next(self):
                if seen == 1 and outcome == "fetch":
                    raise failure
                if seen == 1 and outcome == "cancel":
                    paused.set()
                    await asyncio.Event().wait()
                return await next_row(self)

            monkeypatch.setattr(type(rows), "__anext__", interrupted_next)
            return rows

        def interrupted_parse(day, body):
            nonlocal seen
            if seen == 1 and outcome == "parse":
                raise failure
            seen += 1
            return parse(day, body) if outcome in ("early", "invalid") else None

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "heart_zone", interrupted_parse)
        task = asyncio.create_task(reader.adjacent_hearts(session, DAY, DAY))
        try:
            if outcome == "cancel":
                await asyncio.wait_for(paused.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome in ("parse", "fetch"):
                with pytest.raises(ValueError) as caught:
                    await task
                assert caught.value is failure
            else:
                found = await task
                assert len(found) == (1 if outcome == "early" else 0)
                assert seen == (0 if outcome == "empty" else 1 if outcome == "early" else 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(opened) == (1 if side == -1 and outcome in ("parse", "fetch", "cancel") else 2)
        assert all(result.closed and result.close.await_count == 1 for result in opened)
        assert session.get_transaction() is transaction and transaction.is_active
        assert await snapshot(session) == before
        assert not any(getattr(session, m).await_count for m in ("commit", "rollback", "close"))
