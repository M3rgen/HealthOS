"""Production-ingested sleep keeps small overlap events, never whole raw documents."""

import asyncio
import csv
import gc
import io
import tracemalloc
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, PHONE, T0, sleep, snapshot, store
from sqlalchemy import event
from support.postgres_reads import read_statements, rows_in_flight, socket_buffers
from test_google_platform_daily import DST_DAYS
from test_sleep_corrections import api as api

from app.core.config import get_settings
from app.parsers.sleep import SLEEP_NUMERIC_ERROR
from app.services.interpretation import google_daily_readers as reader
from app.services.readers import DashboardSources, daily_window
from app.services.readers.reading import ArchiveBounds
from app.services.umay import codex_health

ZONE = ZoneInfo("UTC")
WINDOW = daily_window(DAY, 1, ZONE)


async def read(session):
    reads = {}
    await reader.read_sleep(session, WINDOW, ZONE, DashboardSources(), reads)
    return reads


@pytest.mark.parametrize("payload_bytes,stage_count", [(0, 0), (32768, 128)])
async def test_memory_is_batch_plus_small_overlap_events(
    sessions, monkeypatch, payload_bytes, stage_count
):
    batch = 16
    monkeypatch.setattr(reader, "_SLEEP_BATCH", batch)
    for first, count in ((0, 128), (128, 2048)):
        await store(
            sessions,
            "sleep",
            *[
                sleep(
                    str(i),
                    start=T0 + timedelta(seconds=i),
                    end=T0 + timedelta(seconds=i + 3600),
                    syntheticPadding="x" * payload_bytes,
                    stages=[{"type": "AWAKE", "duration": "1s"}] * stage_count,
                )
                for i in range(first, count)
            ],
        )
        async with sessions() as session:
            await read(session)  # exclude ingestion, SQL compilation and eligibility warmup
            gc.collect()
            tracemalloc.start()
            try:
                result = await read(session)
                retained, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            value = result["FITBIT"].days[DAY]["sleep_seconds"]
            assert value.value == pytest.approx((3600 + count - 1) * (3600 - stage_count) / 3600)
            assert value.sleep_assumptions == (
                ("overlap_estimated",) if stage_count else ("awake_unknown",)
            )
            assert not session.identity_map and not session.new and not session.dirty
        # Output has one day. Sweep events/heap are O(count); decoded JSON is O(batch).
        event_budget = count * 1536
        batch_budget = rows_in_flight(sessions, batch) * (payload_bytes + stage_count * 512 + 4096)
        print(f"sleep {count=} {payload_bytes=} {stage_count=}: {retained=} B, {peak=} B")
        assert retained < 1536 * 1024  # includes fixed query/driver allocations
        assert peak < 1024 * 1024 + socket_buffers(sessions) + event_budget + batch_budget


@pytest.mark.parametrize("consumer", ["daily", "bounds"])
@pytest.mark.parametrize("failure", [None, "parse", "evaluate", "fetch", "cancel", "consumer"])
async def test_stream_closes_and_caller_keeps_transaction(sessions, monkeypatch, failure, consumer):
    await store(
        sessions,
        "sleep",
        *(
            sleep(
                str(i), start=T0 + timedelta(days=i, hours=1), end=T0 + timedelta(days=i, hours=3)
            )
            for i in range(7)
        ),
    )
    monkeypatch.setattr(reader, "_SLEEP_BATCH", 1)
    async with sessions.begin() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        for method in ("commit", "rollback", "close"):
            monkeypatch.setattr(session, method, AsyncMock(wraps=getattr(session, method)))
        opened, seen = [], 0
        stream, parse, add = (
            session.stream_scalars,
            reader.sleep_window,
            reader.SleepAccumulator.add,
        )

        async def tracked(*args, **kwargs):
            assert kwargs["execution_options"]["yield_per"] == 1
            result = await stream(*args, **kwargs)
            opened.append(result)
            monkeypatch.setattr(result, "close", AsyncMock(wraps=result.close))
            next_row = type(result).__anext__

            async def interrupted_next(result):
                if seen == 2 and failure == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)  # actual task cancellation between driver fetches
                if seen == 2 and failure == "fetch":
                    raise ValueError("synthetic fetch failure")
                return await next_row(result)

            monkeypatch.setattr(type(result), "__anext__", interrupted_next)
            return result

        def interrupted_parse(*args):
            nonlocal seen
            seen += 1
            if seen == 2 and failure == "parse":
                raise ValueError("synthetic parse failure")
            return parse(*args)

        def interrupted_add(*args):
            if seen == 2 and failure == "evaluate":
                raise ValueError("synthetic evaluation failure")
            return add(*args)

        statements = []
        engine = session.get_bind()

        def trace(conn, cursor, statement, parameters, context, many):
            statements.append(statement)

        monkeypatch.setattr(session, "stream_scalars", tracked)
        monkeypatch.setattr(reader, "sleep_window", interrupted_parse)
        monkeypatch.setattr(reader.SleepAccumulator, "add", interrupted_add)
        if failure == "consumer":

            class BrokenMeasurement:
                def __getattr__(self, name):
                    raise ValueError("synthetic consumer failure")

            monkeypatch.setattr(reader, "DailyValue", lambda *args: BrokenMeasurement())

        async def consume():
            if consumer == "bounds":
                bounds = await reader.archive_bounds(
                    session, "sleep", DAY + timedelta(days=6), ZONE
                )
                assert bounds == ArchiveBounds(DAY, DAY + timedelta(days=6))
            else:
                reads = {}
                await reader.read_sleep(
                    session,
                    daily_window(DAY + timedelta(days=6), 7, ZONE),
                    ZONE,
                    DashboardSources(),
                    reads,
                )
                assert len(reads["FITBIT"].days) == 7
                assert {day["sleep_seconds"].value for day in reads["FITBIT"].days.values()} == {
                    7200
                }

        event.listen(engine, "before_cursor_execute", trace)
        try:
            if failure is None:
                await consume()
            else:
                with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                    await consume()
        finally:
            event.remove(engine, "before_cursor_execute", trace)
        assert seen >= 2 and len(opened) == len(read_statements(statements)) == 1
        opened[0].close.assert_awaited_once()
        assert opened[0].closed and session.get_transaction() is transaction
        assert session.in_transaction() and transaction.is_active
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted
        assert not any(getattr(session, m).await_count for m in ("commit", "rollback", "close"))


@pytest.mark.parametrize("batch", [1, 3, 100])
@pytest.mark.parametrize("reverse", [False, True])
async def test_overlap_shapes_and_assumption_order_across_batches(
    sessions, monkeypatch, batch, reverse
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", batch)
    # Nested, crossing and disjoint spans: asleep fractions 1/2, 1/4, 1 and 1.
    records = [
        sleep("outer", start=T0, end=T0 + timedelta(hours=4), summary={"minutesAsleep": 120}),
        sleep("inner", summary={"minutesAsleep": 30}),
        sleep("cross", start=T0 + timedelta(hours=2), end=T0 + timedelta(hours=5)),
        sleep(
            "disjoint", start=T0 + timedelta(hours=6), end=T0 + timedelta(hours=7), duration="3600s"
        ),
        sleep(
            "zero",
            start=T0 + timedelta(hours=8),
            end=T0 + timedelta(hours=9),
            summary={"minutesAsleep": 0},
        ),
        sleep("phone", source=PHONE, summary={"minutesAsleep": 0}),
    ]
    await store(sessions, "sleep", *(reversed(records) if reverse else records))
    async with sessions() as session:
        before = await snapshot(session)
        result = await read(session)
        band, phone = (
            result[source].days[DAY]["sleep_seconds"] for source in ("FITBIT", "HEALTH_KIT")
        )
        assert band.value == 12600 and phone.value == 0
        assert band.sleep_assumptions == (
            "duration_from_payload",
            "awake_unknown",
            "overlap_estimated",
        )
        assert phone.sleep_assumptions == ()
        assert await reader.archive_bounds(session, "sleep", DAY, ZONE) == ArchiveBounds(DAY, DAY)
        assert await snapshot(session) == before


@pytest.mark.parametrize("zone_name,day,hours,shares", DST_DAYS)
async def test_wake_day_midnight_and_dst_keep_platforms_separate(
    sessions, monkeypatch, zone_name, day, hours, shares
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", 1)
    zone = ZoneInfo(zone_name)
    midnight = datetime.combine(day, datetime.min.time(), zone)
    start = (midnight - timedelta(hours=2)).astimezone(ZONE)
    end = (midnight + timedelta(hours=8)).astimezone(ZONE)
    inner_start = (midnight + timedelta(hours=1)).astimezone(ZONE)
    inner_end = (midnight + timedelta(hours=4)).astimezone(ZONE)
    window = daily_window(day, 1, zone)
    await store(
        sessions,
        "sleep",
        sleep(
            "outer",
            start=start,
            end=end,
            summary={"minutesAsleep": (end - start).total_seconds() / 120},
        ),
        sleep("inner", start=inner_start, end=inner_end, summary={"minutesAsleep": 0}),
        sleep("phone", start=start, end=end, source=PHONE),
        sleep(
            "midnight",
            start=start,
            end=window.start,
            source={"platform": "GOOGLE_FIT"},
            summary={"minutesAsleep": 30},
        ),
        sleep("exclusive", start=window.stop - timedelta(hours=1), end=window.stop),
    )
    async with sessions() as session:
        before = await snapshot(session)
        reads = {}
        await reader.read_sleep(session, window, zone, DashboardSources(), reads)
        assert all(set(read.days) == {day} for read in reads.values())
        band = reads["FITBIT"].days[day]["sleep_seconds"]
        assert band.value == 12600 and band.sleep_assumptions == ("overlap_estimated",)
        phone = reads["HEALTH_KIT"].days[day]["sleep_seconds"]
        assert phone.value == (end - start).total_seconds()
        assert phone.sleep_assumptions == ("awake_unknown",)
        assert reads["GOOGLE_FIT"].days[day]["sleep_seconds"].value == 1800
        assert await reader.archive_bounds(session, "sleep", day, zone) == ArchiveBounds(day, day)
        assert await snapshot(session) == before


@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT"])
@pytest.mark.parametrize("bad_duration", ["-1s", "1e309s"])
async def test_corrections_and_recovery_agree_in_public_daily_outputs(
    api, sessions, monkeypatch, preferred, bad_duration
):
    monkeypatch.setattr(reader, "_SLEEP_BATCH", 1)
    monkeypatch.setattr(
        get_settings(), "source_precedence_health", f"{preferred},FITBIT,HEALTH_KIT"
    )
    await store(
        sessions,
        "sleep",
        sleep("outer", start=T0, end=T0 + timedelta(hours=4), summary={"minutesAsleep": 120}),
        sleep("inner", summary={"minutesAsleep": 30}),
        sleep("phone", source=PHONE, summary={"minutesAsleep": 0}),
    )
    # Invalid raw duration must invalidate the day, even with sound normalized evidence.
    corrected = sleep(
        "inner", start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=3), duration=bad_duration
    )
    recovered = sleep(
        "inner",
        start=T0 - timedelta(hours=1),
        end=T0 + timedelta(hours=3),
        summary={"minutesAsleep": 60},
    )
    for record, band_value, codes in (
        (None, 5400, ["overlap_estimated"]),
        (corrected, None, [SLEEP_NUMERIC_ERROR]),
        (recovered, 5400, ["overlap_estimated"]),
    ):
        if record:
            stats = await store(sessions, "sleep", record)
            assert stats.updated == stats.versions_kept == 1
            assert stats.normalization_skipped == int(record is corrected and bad_duration == "-1s")
        expected = band_value if preferred == "FITBIT" and band_value is not None else 0
        assumptions = codes if preferred == "FITBIT" and band_value is not None else []
        async with sessions() as session:
            before = await snapshot(session)
            values = (await read(session))["FITBIT"].days[DAY]["sleep_seconds"]
            assert values.value == band_value and list(values.sleep_assumptions) == codes
        params = {"end_date": str(DAY), "days": 1}
        response = await api.get("/api/dashboard", params=params)
        assert response.status_code == 200, response.text
        [day] = response.json()["days"]
        assert day["sleep_seconds"] == expected and day["sleep_assumptions"] == assumptions
        history = (await api.get("/api/dashboard/history", params={"metric": "sleep"})).json()
        assert next(row["value"] for row in history["days"] if row["date"] == str(DAY)) == expected
        exported = await api.get("/api/dashboard/export.csv", params=params)
        [row] = csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff")))
        assert float(row["sleep_seconds"]) == expected
        daily = await codex_health.read_health(
            sessions,
            {"action": "daily", "start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"},
        )
        assert daily["days"][0]["sleep_seconds"] == expected
        assert daily["days"][0]["sleep_assumptions"] == assumptions
        async with sessions() as session:
            assert await snapshot(session) == before
