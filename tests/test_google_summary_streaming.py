"""Production-ingested daily summaries keep candidates, not the entire raw archive."""

import asyncio
import gc
import tracemalloc
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from derived_records import DAY, PHONE, snapshot, store, summary
from support.numeric_outputs import api as api
from test_google_platform_daily import METRICS, candidates, project

from app.services.interpretation import google_daily_readers as reader
from app.services.readers import DashboardSources, daily_window

WINDOW = daily_window(DAY, 1, ZoneInfo("UTC"))


@pytest.mark.parametrize("kind,key,field,model", METRICS[2:])
async def test_summary_memory_is_bounded_by_batch_not_record_count(
    sessions, monkeypatch, kind, key, field, model
):
    monkeypatch.setattr(reader, "_SUMMARY_BATCH", 64)

    async def read(count):
        async with sessions() as session:
            stream = AsyncMock(wraps=session.stream)
            session.stream = stream
            reads = {}
            await reader.read_summaries(session, WINDOW, DashboardSources(metrics=(key,)), reads)
            assert stream.await_count == 1  # no query per candidate or per batch
            assert not session.identity_map and not session.new and not session.dirty
        assert set(reads) == {"FITBIT", "HEALTH_KIT"}
        assert reads["FITBIT"].days[DAY][key].value == count - 1
        assert reads["HEALTH_KIT"].days[DAY][key].value == 80  # oldest candidate still counts

    async def measure(count):
        gc.collect()
        tracemalloc.start()
        try:
            await read(count)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    await store(sessions, kind, summary(kind, "00000", source=PHONE, **{field: 80}))
    peaks = []
    for first, count in ((1, 512), (512, 8192)):
        await store(
            sessions, kind, *(summary(kind, f"{i:05}", **{field: i}) for i in range(first, count))
        )
        await read(count)  # ingestion, SQL compilation and eligibility warmup are not traced
        peaks.append(await measure(count))
    print(f"{kind} peak: 512={peaks[0]} B; 8192={peaks[1]} B")
    assert peaks[1] < peaks[0] + 256 * 1024  # tolerates allocator noise; .all() retains MBs


@pytest.mark.parametrize(
    "kind,key,fields",
    [
        ("daily-resting-heart-rate", "resting_bpm", ("beatsPerMinute",)),
        (
            "daily-heart-rate-variability",
            "hrv_ms",
            (
                "averageHeartRateVariabilityMilliseconds",
                "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds",
                "rmssd",
            ),
        ),
        ("daily-oxygen-saturation", "spo2", ("averagePercentage", "percentage", "average")),
    ],
)
async def test_ties_refetch_and_field_priority_survive_batch_boundaries(
    api, sessions, monkeypatch, clock, kind, key, fields
):
    monkeypatch.setattr(reader, "_SUMMARY_BATCH", 1)
    older = summary(kind, "a", **{field: 60 + i for i, field in enumerate(fields)})
    newer = summary(kind, "z", **{field: 70 + i for i, field in enumerate(fields)})
    # Reverse ingestion order at the same time: Google id, not insertion order, breaks ties.
    await store(
        sessions, kind, newer, older, summary(kind, "phone", source=PHONE, **{fields[0]: 80})
    )
    for record, expected in (
        (None, 70),
        (older, 60),
        (summary(kind, "a", **{f: "NaN" if i == 0 else 91 for i, f in enumerate(fields)}), None),
        *(
            (summary(kind, "a", **{f: v if i == 0 else 91 for i, f in enumerate(fields)}), None)
            for v in (-1, "-1", "-1e-999999", "-1e999999")
        ),
        *(
            (summary(kind, "a", **dict.fromkeys(fields, v)), float(v))
            for v in (0, "0", "-0", "-0e999999", "1e-100", "1e100")
        ),
    ):
        if record is not None:
            clock.advance(seconds=1)
            await store(sessions, kind, record)
        day, _ = await project(sessions, monkeypatch)
        assert candidates(day, key) == {"HEALTH_KIT": 80} | (
            {"FITBIT": expected} if expected is not None else {}
        )
        response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
        assert response.status_code == 200
        assert response.json()["days"][0][key] == (80 if expected is None else expected)


@pytest.mark.parametrize("failure", [None, ValueError, asyncio.CancelledError])
async def test_summary_cursor_closes_on_completion_processing_error_and_cancellation(
    sessions, monkeypatch, failure
):
    kind = "daily-resting-heart-rate"
    await store(sessions, kind, *(summary(kind, str(i), beatsPerMinute=i) for i in range(4)))
    monkeypatch.setattr(reader, "_SUMMARY_BATCH", 1)
    async with sessions() as session:
        before = await snapshot(session)
        opened, stream, parse = [], session.stream, reader.payload_for

        async def tracked(*args, **kwargs):
            result = await stream(*args, **kwargs)
            opened.append(result)
            if failure is asyncio.CancelledError:
                next_row = type(result).__anext__

                async def cancelled_next(result):
                    if reads:
                        asyncio.current_task().cancel()
                        await asyncio.sleep(0)  # cancel between fetches, preserving SQLite's DB
                    return await next_row(result)

                monkeypatch.setattr(type(result), "__anext__", cancelled_next)
            return result

        def interrupted(*args):
            if failure is ValueError and reads:  # interrupt after a candidate was processed
                raise failure()
            return parse(*args)

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "payload_for", interrupted)
        reads = {}
        if failure is None:
            await reader.read_summaries(session, WINDOW, DashboardSources(), reads)
        else:
            with pytest.raises(failure):
                await reader.read_summaries(session, WINDOW, DashboardSources(), reads)
        assert len(opened) == 1 and opened[0].closed
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted
        await reader.read_summaries(session, WINDOW, DashboardSources(metrics=()), {})
        assert len(opened) == 1  # no requested summaries means no cursor/query
