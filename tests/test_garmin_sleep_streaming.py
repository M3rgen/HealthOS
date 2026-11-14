"""Completed Garmin wake days release overlap events; bounds retain only two dates."""

import asyncio
import weakref
from collections import defaultdict
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event
from test_garmin_sleep_validity import snapshot
from test_garmin_step_validity import only_select

from app.models.garmin import GarminRawRecord
from app.models.health import SleepSession
from app.services.interpretation.sleep_estimate import sleep_estimate
from app.services.providers import garmin_readers
from app.services.providers import garmin_sleep_values as reader

START = datetime(2026, 1, 1, tzinfo=UTC)
ZONE = ZoneInfo("UTC")


def night(key, end, *, span=3600, awake=1800):
    return SleepSession(
        provider="GARMIN",
        google_data_point_id=key,
        source_record_id=f"sleep:{key}",
        start_time=end - timedelta(seconds=span),
        end_time=end,
        duration_seconds=span,
        awake_seconds=awake,
    )


async def read_bounds(session, today, zone=ZONE):
    return await garmin_readers.archive_bounds(session, "sleep", today, zone)


@pytest.mark.parametrize("batch", [1, 7, 1000])
@pytest.mark.parametrize("invalid", ["source", "normalized"])
async def test_sparse_archive_matches_whole_archive_estimator(
    sessions, monkeypatch, batch, invalid
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", batch)
    groups = defaultdict(list)
    rows = []
    for offset in (0, 3, 80, 900):
        begin = START + timedelta(days=offset)
        # Nested, crossing, disjoint, zero and nonpositive intervals on one wake day.
        for i, (a, b, awake) in enumerate(
            ((0, 4, 7200), (1, 3, 5400), (2, 5, 0), (6, 7, 0), (8, 9, 3600), (10, 10, 0))
        ):
            row = night(
                f"{offset}-{i}", begin + timedelta(hours=b), span=(b - a) * 3600, awake=awake
            )
            rows.append(row)
            groups[row.end_time.date()].append((row, None))
    bad_days = {START.date(), (START + timedelta(days=80)).date(), rows[-1].end_time.date()}
    # Invalid evidence arrives after all the valid sessions, possibly in another batch.
    async with sessions.begin() as session:
        session.add_all(list(reversed(rows)))
        for i, day in enumerate(sorted(bad_days)):
            row = night(
                f"bad-{i}", datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=23)
            )
            if invalid == "normalized":
                row.awake_seconds = -1
            else:
                session.add(
                    GarminRawRecord(
                        kind="sleep",
                        key=f"bad-{i}",
                        body={"sleepTimeSeconds": "-1e-10000"},
                        body_sha256="a" * 64,
                        first_seen_at=START,
                    )
                )
            session.add(row)
    expected = {
        day: sleep_estimate(pairs).seconds for day, pairs in groups.items() if day not in bad_days
    }
    assert list(expected.values()) == [12600]
    async with sessions() as session:
        assert (
            await reader.sleep_days(session, START, START + timedelta(days=901), ZONE) == expected
        )
        bounds = await read_bounds(session, rows[-1].end_time.date())
        assert bounds.earliest == min(expected) and bounds.latest == max(expected)


@pytest.mark.parametrize(
    "zone_name,origin",
    [
        ("America/Los_Angeles", "2026-03-08T07:30:00+00:00"),
        ("America/Los_Angeles", "2026-11-01T07:30:00+00:00"),
        ("America/Goose_Bay", "2009-11-01T02:30:00+00:00"),
        ("Pacific/Kwajalein", "1969-09-30T12:00:00+00:00"),
        ("Pacific/Apia", "2011-12-30T09:00:00+00:00"),
    ],
)
@pytest.mark.parametrize("batch", [1, 3])
async def test_offset_changes_keep_repeated_wake_days_open(
    sessions, monkeypatch, zone_name, origin, batch
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", batch)
    zone, begin = ZoneInfo(zone_name), datetime.fromisoformat(origin)
    groups = defaultdict(list)
    rows = [night(f"session-{i}", begin + timedelta(minutes=30 * i)) for i in range(100)]
    for row in rows:
        groups[row.end_time.astimezone(zone).date()].append((row, None))
    expected = {day: sleep_estimate(pairs).seconds for day, pairs in groups.items()}
    if zone_name == "America/Goose_Bay":
        assert [row.end_time.astimezone(zone).day for row in rows[:4]] == [31, 1, 31, 1]
    async with sessions.begin() as session:
        session.add_all(list(reversed(rows)))
    async with sessions() as session:
        assert (
            await reader.sleep_days(
                session, begin - timedelta(days=1), begin + timedelta(days=4), zone
            )
            == expected
        )
        bounds = await read_bounds(session, max(expected), zone)
        assert bounds.earliest == min(expected) and bounds.latest == max(expected)


async def test_live_events_depend_on_daily_density_not_archive_length(sessions, monkeypatch):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", 16)
    original, add = reader.SleepAccumulator, reader.SleepAccumulator.add
    live, peaks = weakref.WeakValueDictionary(), []

    def accumulator():
        value = original()
        live[id(value)] = value
        return value

    def tracked_add(self, row, payload):
        add(self, row, payload)
        peaks.append(
            sum(len(events) for value in live.values() for events in value.events.values())
        )

    monkeypatch.setattr(reader, "SleepAccumulator", accumulator)
    monkeypatch.setattr(original, "add", tracked_add)
    for days, density in ((8, 16), (64, 16), (64, 96)):
        async with sessions.begin() as session:
            for day in range(days):
                first = 16 if days == 64 and density == 96 else 0
                if days == 64 and density == 16 and day < 8:
                    continue
                session.add_all(
                    [
                        night(f"{day}-{i}", START + timedelta(days=day, hours=6, seconds=i))
                        for i in range(first, density)
                    ]
                )
        for bounds_only in (False, True):
            statements = []
            async with sessions() as session:

                def capture(*args, _statements=statements):
                    only_select(*args)
                    assert args[4].execution_options["yield_per"] == 16
                    _statements.append(args[2])

                engine = session.get_bind()
                event.listen(engine, "before_cursor_execute", capture)
                peaks.clear()
                try:
                    if bounds_only:
                        bounds = await read_bounds(session, (START + timedelta(days=days)).date())
                        assert (bounds.earliest, bounds.latest) == (
                            START.date(),
                            (START + timedelta(days=days - 1)).date(),
                        )
                    else:
                        values = await reader.sleep_days(
                            session, START, START + timedelta(days=days), ZONE
                        )
                        assert len(values) == days and set(values.values()) == {
                            (3600 + density - 1) / 2
                        }
                finally:
                    event.remove(engine, "before_cursor_execute", capture)
                assert len(statements) == 1
                assert "ORDER BY sleep_sessions.end_time, sleep_sessions.id" in statements[0]
                assert not session.identity_map and not session.new and not session.dirty
            assert not live
            assert max(peaks) == 4 * density
            print(f"Garmin sleep {days=} {density=} {bounds_only=}: live events={max(peaks)}")


async def test_bounds_does_not_collect_daily_results(sessions, monkeypatch):
    class Value(float):
        pass

    live = weakref.WeakValueDictionary()

    async def daily(*args):
        for i in range(4096):
            value = Value(i)
            live[i] = value
            assert len(live) <= 2, "bounds retained daily result values"
            yield (START + timedelta(days=i)).date(), value

    monkeypatch.setattr(garmin_readers, "iter_sleep_days", daily)
    async with sessions() as session:
        bounds = await read_bounds(session, (START + timedelta(days=4096)).date())
    assert (bounds.earliest, bounds.latest) == (START.date(), (START + timedelta(days=4095)).date())


@pytest.mark.parametrize("consumer", ["values", "bounds", "iterator"])
@pytest.mark.parametrize("failure", [None, "parse", "evaluate", "fetch", "cancel"])
async def test_stream_closes_on_success_error_cancellation_and_early_exit(
    sessions, monkeypatch, consumer, failure
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", 1)
    async with sessions.begin() as session:
        session.add_all([night(str(i), START + timedelta(days=i, hours=6)) for i in range(7)])
    async with sessions.begin() as session:
        before, transaction = await snapshot(session), session.get_transaction()
        stream, negative, result = session.stream, reader._negative, reader.SleepAccumulator.result
        opened, seen = [], 0

        async def tracked(*args, **kwargs):
            rows = await stream(*args, **kwargs)
            opened.append(rows)
            monkeypatch.setattr(rows, "close", AsyncMock(wraps=rows.close))
            next_row = type(rows).__anext__

            async def interrupted_next(self):
                if seen == 2:
                    if failure == "cancel":
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)
                    if failure == "fetch":
                        raise ValueError("synthetic fetch failure")
                return await next_row(self)

            monkeypatch.setattr(type(rows), "__anext__", interrupted_next)
            return rows

        def parse(value):
            nonlocal seen
            seen += 1
            if seen == 2 and failure == "parse":
                raise ValueError("synthetic parse failure")
            return negative(value)

        def evaluate(self):
            if failure == "evaluate":
                raise ValueError("synthetic evaluation failure")
            return result(self)

        async def read():
            if consumer == "bounds":
                return await read_bounds(session, (START + timedelta(days=7)).date())
            if consumer == "values":
                return await reader.sleep_days(session, START, START + timedelta(days=7), ZONE)
            async with aclosing(
                reader.iter_sleep_days(session, START, START + timedelta(days=7), ZONE)
            ) as days:
                async for _ in days:
                    break

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "_negative", parse)
        monkeypatch.setattr(reader.SleepAccumulator, "result", evaluate)
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            if failure is None:
                await read()
            else:
                with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                    await read()
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
        assert len(opened) == 1 and opened[0].closed
        opened[0].close.assert_awaited_once()
        assert session.get_transaction() is transaction and transaction.is_active
        assert await snapshot(session) == before
