"""Coverage consumes production-ingested synthetic payloads without retaining the archive."""

import asyncio
import gc
import tracemalloc
from contextlib import aclosing
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from coverage_source_records import KINDS
from coverage_source_records import record as source_record
from derived_records import BAND, DAY, PHONE, T0, point, sleep, snapshot, steps, store, summary
from sqlalchemy import select
from support.coverage_fixtures import fetch, query

from app.google_health.data_types import REGISTRY, RecordKind
from app.models.state import RawHealthData
from app.services.synchronization import coverage_raw, coverage_sources, retrieval_coverage
from app.services.synchronization import coverage_reading as reader
from app.services.synchronization.coverage_types import Window
from app.services.umay.codex_health import read_health

METRICS = tuple(metric for metric in reader.METRICS if metric.data_type is not None)
WINDOW = Window(DAY, DAY + timedelta(days=1), DAY + timedelta(days=3), ZoneInfo("UTC"))


def record(metric, index, invalid=False, offset=None):
    kind, name = metric.data_type, str(index)
    at = T0 + timedelta(days=(index // 2) % 2 if offset is None else offset, hours=2)
    source = BAND if index % 2 == 0 else PHONE
    value = "NaN" if invalid else 0  # a measured zero still establishes presence
    if kind == "steps":
        return steps(name, count=value, start=at, source=source)
    if kind == "sleep":
        return sleep(
            name,
            start=at + timedelta(hours=int(invalid) * 2),
            end=at + timedelta(hours=1),
            source=source,
        )
    if metric.read == "raw_daily":
        return summary(
            kind,
            name,
            day=at.date(),
            source=source,
            **{field: value if i == 0 else 93 for i, field in enumerate(metric.fields)},
        )
    field = "weightGrams" if kind == "weight" else metric.fields[0]
    return point(kind, name, {"sampleTime": {"physicalTime": at.isoformat()}, field: value}, source)


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.key)
async def test_raw_payload_memory_is_bounded_by_batch(sessions, monkeypatch, metric):
    # Only google_streams is traced: preparation, source/day growth and the card's other
    # readers are outside this claim. Both archive sizes use two sources and two days.
    monkeypatch.setattr(reader, "_NORMALIZED_BATCH", 64)
    monkeypatch.setattr(coverage_raw, "_RAW_BATCH", 64)

    async def read(count):
        async with sessions() as session:
            streams, invalid = await reader.google_streams(session, metric, WINDOW)
            assert invalid == 0 and len(streams) == 2
            assert sum(stream.records for stream in streams.values()) == count
            assert all(stream.days == {WINDOW.first, WINDOW.end} for stream in streams.values())
            assert not session.identity_map and not session.new and not session.dirty

    peaks = []
    for first, count in ((0, 512), (512, 8192)):
        await store(sessions, metric.data_type, *(record(metric, i) for i in range(first, count)))
        await read(count)  # warm SQL compilation and eligibility outside measurement
        gc.collect()
        tracemalloc.start()
        try:
            await read(count)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    print(f"{metric.key} peak: 512={peaks[0]} B; 8192={peaks[1]} B")
    assert peaks[1] < peaks[0] + 256 * 1024  # buffered JSON would retain many MBs


@pytest.mark.parametrize("metric", METRICS, ids=lambda metric: metric.key)
async def test_batches_preserve_counts_corrections_sources_exclusions_and_receipts(
    sessions, monkeypatch, metric
):
    monkeypatch.setattr(reader, "_NORMALIZED_BATCH", 7)
    monkeypatch.setattr(coverage_raw, "_RAW_BATCH", 7)
    await fetch(
        sessions,
        start=WINDOW.start,
        end=WINDOW.stop,
        kind=metric.data_type,
        points=[record(metric, i) for i in range(137)],
    )
    await store(
        sessions,
        metric.data_type,
        *(record(metric, i, invalid=True) for i in (0, 7, 136)),
        record(metric, 200, offset=-1),
        record(metric, 201, offset=2),
    )
    async with sessions.begin() as session:
        for i, flag in ((8, "is_quarantined"), (9, "source_deleted_at")):
            row = await session.scalar(
                select(RawHealthData).where(
                    RawHealthData.google_data_point_id == record(metric, i)["name"]
                )
            )
            setattr(row, flag, True if i == 8 else T0)
    async with sessions() as session:
        before = await snapshot(session)
        streams, invalid = await reader.google_streams(session, metric, WINDOW)
        assert invalid == 3  # invalid corrections never revive retained normalized values
        assert sorted(stream.records for stream in streams.values()) == [66, 66]
    card = await read_health(
        sessions, query(WINDOW.first, WINDOW.end, timezone="UTC", data_type=metric.data_type)
    )
    result = card["metrics"][0]
    assert result["value_days"] == 2 and result["days_without_value"] == 0
    assert result["sampling"]["sample_count"] == 132
    assert result["exclusions"]["invalid_shape"] == 3
    assert result["exclusions"]["quarantined"] == result["exclusions"]["source_deleted"] == 1
    regimes = result["source_regimes"]
    assert {row["platform"] for row in regimes} == {"FITBIT", "HEALTH_KIT"}
    assert len({row["source"] for row in regimes}) == 2
    assert all(row["record_count"] == 66 and row["value_days"] == 2 for row in regimes)
    assert result["retrieval"]["state"] == "receipts"
    assert result["retrieval"]["completed_with_records_days"] == 2
    assert result["retrieval"]["unknown_days"] == 0
    async with sessions() as session:
        assert await snapshot(session) == before  # includes raw versions and receipt history


@pytest.mark.parametrize("metric", [METRICS[0], METRICS[2], METRICS[-1]], ids=lambda m: m.key)
@pytest.mark.parametrize("failure", [None, "producer", "consumer", "cancel", "early"])
async def test_every_opened_cursor_closes(sessions, monkeypatch, metric, failure):
    await store(sessions, metric.data_type, *(record(metric, i) for i in range(5)))
    monkeypatch.setattr(reader, "_NORMALIZED_BATCH", 1)
    monkeypatch.setattr(coverage_raw, "_RAW_BATCH", 1)
    async with sessions() as session:
        before = await snapshot(session)
        opened, stream, seen = [], session.stream, 0
        group = reader.stream_of
        parse = (
            coverage_raw.payload_for
            if metric.read not in reader.NORMALIZED
            else reader.current_steps
        )

        async def tracked(*args, **kwargs):
            assert kwargs["execution_options"]["yield_per"] == 1
            result = await stream(*args, **kwargs)
            opened.append(result)
            if failure == "cancel":
                next_row = type(result).__anext__

                async def cancelled_next(result):
                    if seen:
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)  # real task cancellation between fetches
                    return await next_row(result)

                monkeypatch.setattr(type(result), "__anext__", cancelled_next)
            return result

        def grouped(*args):
            nonlocal seen
            seen += 1
            if failure == "consumer" and seen == 2:
                raise ValueError("synthetic source grouping error")
            return group(*args)

        def parsed(*args):
            if failure == "producer" and seen:
                raise ValueError("synthetic payload processing error")
            return parse(*args)

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "stream_of", grouped)
        if metric.read in reader.NORMALIZED:
            monkeypatch.setattr(reader, "current_steps", parsed)
        else:
            monkeypatch.setattr(coverage_raw, "payload_for", parsed)
        if failure == "early":
            rows = (
                reader._normalized_records(session, metric, WINDOW)
                if metric.read in reader.NORMALIZED
                else coverage_raw.raw_value_days(session, metric, WINDOW)
            )
            async with aclosing(rows):
                async for _ in rows:
                    break
        elif failure is None:
            await reader.google_streams(session, metric, WINDOW)
        else:
            error = asyncio.CancelledError if failure == "cancel" else ValueError
            with pytest.raises(error):
                await reader.google_streams(session, metric, WINDOW)
        assert len(opened) == 1 and all(result.closed for result in opened)
        assert await snapshot(session) == before
        assert (
            not session.identity_map
            and not session.new
            and not session.dirty
            and not session.deleted
        )


@pytest.mark.parametrize("kind", ["heart-rate", "exercise"])
async def test_general_source_json_memory_stays_bounded(sessions, kind):
    # Preparation and SQL warmup are outside tracing. Two sources/two days stay fixed;
    # source/day summary growth is a separate cost. Use the production batch size.
    sources = [dict(source, padding="x" * 8192) for source in (BAND, PHONE)]

    async def read(count):
        async with sessions() as session:
            values, regimes, _ = await retrieval_coverage.presence(session, REGISTRY[kind], WINDOW)
            assert values == {WINDOW.first, WINDOW.end} and len(regimes) == 2
            assert sum(row["record_count"] for row in regimes) == count
            assert all(row["value_days"] == 2 for row in regimes)
            assert not session.identity_map and not session.new and not session.dirty

    peaks = []
    for first, count in ((0, 512), (512, 4096)):
        await store(
            sessions,
            kind,
            *(source_record(kind, i, source=sources[i % 2]) for i in range(first, count)),
        )
        await read(count)
        gc.collect()
        tracemalloc.start()
        try:
            await read(count)
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    print(f"{kind} source peak: 512={peaks[0]} B; 4096={peaks[1]} B")
    assert peaks[1] < peaks[0] + 512 * 1024  # buffering would retain >32 MB


@pytest.mark.parametrize("kind", ["heart-rate", "exercise"])
@pytest.mark.parametrize("failure", [None, "consumer", "fetch", "cancel"])
async def test_general_source_cursor_lifetime(sessions, monkeypatch, kind, failure):
    from sqlalchemy.exc import OperationalError

    await store(sessions, kind, *(source_record(kind, i) for i in range(137)))
    monkeypatch.setattr(coverage_sources, "_SOURCE_BATCH", 7)
    async with sessions() as session:
        before = await snapshot(session)
        transaction = session.get_transaction()
        opened, stream, grouped = [], session.stream, coverage_sources.stream_of
        fetched, paused = 0, asyncio.Event()

        async def tracked(statement, **kwargs):
            assert kwargs["execution_options"] == {"yield_per": 7}
            columns = list(statement.selected_columns)
            assert len(columns) == 2 and columns[1].name == "source_start_time"
            assert columns[0].left.name == "raw_json" and columns[0].right.value == "dataSource"
            result = await stream(statement, **kwargs)
            opened.append(result)
            next_row = type(result).__anext__

            async def next_or_fail(result):
                nonlocal fetched
                # Pass asyncpg's minimum 50-row prefetch and several requested batches.
                if fetched == 70:
                    if failure == "fetch":
                        raise OperationalError("synthetic fetch", None, RuntimeError("driver"))
                    if failure == "cancel":
                        paused.set()
                        await asyncio.Event().wait()
                fetched += 1
                return await next_row(result)

            monkeypatch.setattr(type(result), "__anext__", next_or_fail)
            return result

        def group(*args):
            if failure == "consumer" and fetched == 70:
                raise ValueError("synthetic grouping error")
            return grouped(*args)

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(coverage_sources, "stream_of", group)
        task = asyncio.create_task(retrieval_coverage.presence(session, REGISTRY[kind], WINDOW))
        if failure == "cancel":
            await asyncio.wait_for(paused.wait(), timeout=10)
            task.cancel()
        error = {
            "fetch": OperationalError,
            "consumer": ValueError,
            "cancel": asyncio.CancelledError,
        }
        if failure:
            with pytest.raises(error[failure]):
                await task
        else:
            await task
            assert fetched > 137
        assert len(opened) == 1 and opened[0].closed
        assert session.get_transaction() is transaction and transaction.is_active
        assert await snapshot(session) == before
        assert not session.identity_map and not session.new and not session.dirty


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("zone", ["America/New_York", "Europe/Brussels"])
async def test_general_source_dates_and_half_open_window(sessions, kind, zone):
    window = Window(WINDOW.first, WINDOW.end, WINDOW.today, ZoneInfo(zone))
    spec = REGISTRY[kind]
    # Daily coordinates are UTC dates, even when the local day starts on another date.
    lower, upper = (
        (T0, T0 + timedelta(days=2))
        if spec.kind == RecordKind.daily
        else (window.start, window.stop)
    )
    stamps = [
        lower - timedelta(microseconds=1),
        lower,
        lower + timedelta(days=1),
        upper - timedelta(microseconds=1),
        upper,
    ]
    async with sessions.begin() as session:
        for i, stamp in enumerate(stamps):
            # Sleep summaries must use the end, irrespective of an old/different start.
            start = lower - timedelta(days=5) if kind == "respiratory-rate-sleep-summary" else stamp
            session.add(
                RawHealthData(
                    data_type=kind,
                    google_data_point_id=str(i),
                    source_start_time=start,
                    source_end_time=stamp,
                    raw_json={"dataSource": BAND},
                    payload_hash="synthetic",
                    first_seen_at=T0,
                    last_seen_at=T0,
                )
            )
    async with sessions() as session:
        values, regimes, _ = await retrieval_coverage.presence(session, spec, window)
    assert values == {window.first, window.end} and len(regimes) == 1
    assert regimes[0]["record_count"] == 3 and regimes[0]["value_days"] == 2
