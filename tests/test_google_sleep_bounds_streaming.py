"""Sleep bounds keep only document batches and overlap events of unfinished days."""

import gc
import tracemalloc
import weakref
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, T0, sleep, store
from sqlalchemy import event
from support.postgres_reads import is_plan_control, socket_buffers

from app.services.interpretation import google_daily_readers as reader
from app.services.interpretation.sleep_estimate import sleep_estimate
from app.services.readers import DashboardSources, daily_window
from app.services.readers.reading import ArchiveBounds


async def test_live_events_memory_and_queries_are_bounded(sessions, monkeypatch):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", 16)
    original, add, value_type = (
        reader.SleepAccumulator,
        reader.SleepAccumulator.add,
        reader.DailyValue,
    )
    live, values, peaks = weakref.WeakValueDictionary(), weakref.WeakValueDictionary(), []

    def accumulator():
        value = original()
        live[id(value)] = value
        return value

    def tracked_add(self, row, payload):
        add(self, row, payload)
        peaks.append(
            sum(len(events) for value in live.values() for events in value.events.values())
        )

    def measurement(*args):
        value = value_type(*args)
        values[id(value)] = value
        assert len(values) <= 2, "bounds retained daily results"
        return value

    monkeypatch.setattr(reader, "SleepAccumulator", accumulator)
    monkeypatch.setattr(original, "add", tracked_add)
    monkeypatch.setattr(reader, "DailyValue", measurement)
    memory = []
    for days, density in ((8, 16), (128, 16), (128, 48)):
        first_day, first_session = (
            (8, 0) if (days, density) == (128, 16) else (0, 16) if density == 48 else (0, 0)
        )
        await store(
            sessions,
            "sleep",
            *(
                sleep(
                    f"{day}-{i}",
                    start=T0 - timedelta(days=day) + timedelta(seconds=i),
                    end=T0 - timedelta(days=day) + timedelta(seconds=i + 3600),
                    summary={"minutesAsleep": 30},
                    padding="x" * 8192,
                )
                for day in range(first_day, days)
                for i in range(first_session, density)
            ),
        )
        async with sessions() as session:
            await reader.archive_bounds(session, "sleep", DAY, ZoneInfo("UTC"))
            gc.collect()
            statements = []

            def trace(conn, cursor, statement, parameters, context, many, *, captured=statements):
                if is_plan_control(statement):
                    return
                assert statement.lstrip().startswith("SELECT")
                assert context.execution_options["yield_per"] == 16
                captured.append(statement)

            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", trace)
            peaks.clear()
            tracemalloc.start()
            try:
                bounds = await reader.archive_bounds(session, "sleep", DAY, ZoneInfo("UTC"))
                memory.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
                event.remove(engine, "before_cursor_execute", trace)
            assert bounds == ArchiveBounds(DAY - timedelta(days=days - 1), DAY)
            assert len(statements) == 1 and not live and not values
            assert max(peaks) <= 6 * density
            assert not session.identity_map and not session.new and not session.dirty
    print(f"sleep bounds peaks: {memory}")
    assert memory[1] < memory[0] + 512 * 1024 + socket_buffers(sessions)
    assert max(memory) < 3 * 1024 * 1024 + socket_buffers(sessions)


@pytest.mark.parametrize(
    "zone_name,origin",
    [
        ("America/Goose_Bay", "2009-11-01T02:30:00+00:00"),
        ("Pacific/Kwajalein", "1969-09-30T12:00:00+00:00"),
        ("Pacific/Apia", "2011-12-30T09:00:00+00:00"),
    ],
)
@pytest.mark.parametrize("batch", [1, 3])
async def test_repeated_wake_days_stay_open_across_batches(
    sessions, monkeypatch, zone_name, origin, batch
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", batch)
    zone, begin = ZoneInfo(zone_name), datetime.fromisoformat(origin)
    groups, records = defaultdict(list), []
    for i in reversed(range(100)):
        end = begin + timedelta(minutes=30 * i)
        record = sleep(
            str(i), start=end - timedelta(hours=1), end=end, summary={"minutesAsleep": 30}
        )
        span, payload = reader.sleep_window(
            record, begin - timedelta(days=1), begin + timedelta(days=4)
        )
        groups[end.astimezone(zone).date()].append((span, payload))
        records.append(record)
    expected = {day: sleep_estimate(pairs).seconds for day, pairs in groups.items()}
    if zone_name == "America/Goose_Bay":
        assert [(begin + timedelta(minutes=30 * i)).astimezone(zone).day for i in range(4)] == [
            31,
            1,
            31,
            1,
        ]
    await store(sessions, "sleep", *records)
    async with sessions() as session:
        assert await reader.archive_bounds(session, "sleep", max(expected), zone) == ArchiveBounds(
            min(expected), max(expected)
        )
        reads = {}
        await reader.read_sleep(
            session, daily_window(max(expected), 7, zone), zone, DashboardSources(), reads
        )
        assert {
            day: metrics["sleep_seconds"].value for day, metrics in reads["FITBIT"].days.items()
        } == expected
