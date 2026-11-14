"""Raw daily API selects only summary leaves, regardless of archive/sample volume."""

import asyncio
import gc
import json
import tracemalloc
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from garmin_records import daily_summary, heart_rate, sleep, store_page
from sqlalchemy import event, insert, select
from sqlalchemy.dialects import postgresql, sqlite
from test_garmin_normalize import DAY, NOW, raw
from test_garmin_step_validity import insert_raw_json

from app.models.garmin import GarminRawRecord
from app.services.providers import garmin_daily_summary as reader


@pytest.mark.parametrize("token", ["-1e-1000", "-1e-10000"])
async def test_negative_leaf_does_not_hide_other_fields_or_change_raw_text(sessions, token):
    from sqlalchemy import String, cast

    body = (
        '{"sleepTimeSeconds":100,"deepSleepSeconds":TOKEN,"lightSleepSeconds":50,'
        '"remSleepSeconds":20,"awakeSleepSeconds":0,"sleepScores":{"overall":{"value":80}}}'
    )
    body = body.replace("TOKEN", token)
    async with sessions.begin() as session:
        await insert_raw_json(session, "sleep", body)
    async with sessions() as session:
        query = select(cast(GarminRawRecord.body, String))
        archived = (await session.execute(query)).scalars().all()
        # jsonb rewrites the text (key order, -0.000…1) but keeps the exact signed value.
        exact = [json.loads(text, parse_float=Decimal) for text in (*archived, body)]
        assert len(archived) == 1 and exact[0] == exact[1]
        assert exact[0]["deepSleepSeconds"] < 0
        day = (await reader.daily_summaries(session, DAY, DAY))[0]
        assert day["sleep_seconds"] == 100 and day["sleep_score"] == 80
        assert day["sleep_stages_seconds"] == {"deep": None, "light": 50, "rem": 20, "awake": 0}
        assert (await session.execute(query)).scalars().all() == archived


async def test_current_selection_uses_observation_time_ties_and_per_key_legacy(sessions):
    # Newer ids without observations must not override a key with sighting evidence.
    for value, observed in ((10, NOW), (20, NOW + timedelta(minutes=2)), (10, NOW)):
        await store_page(
            sessions, "steps", [{"calendarDate": str(DAY), "totalSteps": value}], observed
        )
    async with sessions.begin() as session:
        session.add(raw("steps", str(DAY), {"totalSteps": 30}, suffix="c"))
        # No sightings: max id wins even if first_seen_at is older.
        session.add(raw("spo2", str(DAY), {"averageSpO2": 90}))
        await session.flush()
        session.add(raw("spo2", str(DAY), {"averageSpO2": 0}, NOW - timedelta(days=1), "b"))
    async with sessions() as session:
        assert (await reader.daily_summaries(session, DAY, DAY))[0]["steps"] == 20
    # The same timestamp breaks ties by observation id, including an A -> B -> A return.
    await store_page(
        sessions,
        "steps",
        [{"calendarDate": str(DAY), "totalSteps": 10}],
        NOW + timedelta(minutes=2),
    )
    async with sessions() as session:
        day = (await reader.daily_summaries(session, DAY, DAY))[0]
    assert day["steps"] == 10 and day["spo2_average"] == 0


async def test_empty_days_order_bounds_and_unread_kinds(sessions):
    async with sessions() as session:
        assert await reader.daily_summaries(session, DAY, DAY + timedelta(days=91)) == []
    async with sessions.begin() as session:
        for offset in (91, 1, -1, 92):
            day = DAY + timedelta(days=offset)
            session.add(raw("sleep", str(day), {}))
        session.add(raw("activities", str(DAY), {"totalSteps": 100}))
    async with sessions() as session:
        actual = await reader.daily_summaries(session, DAY, DAY + timedelta(days=91))
    assert actual == [reader.day_summary(str(DAY + timedelta(days=i)), {}) for i in (1, 91)]


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_projection_is_guarded_numeric_members_and_source_truthiness(dialect):
    query = reader.summary_query(DAY, DAY + timedelta(days=91))
    assert len(query.selected_columns) == 3 + len(reader.PATHS)
    for column in list(query.selected_columns)[2:]:
        sql = str(column.compile(dialect=dialect, compile_kwargs={"literal_binds": False}))
        assert sql.startswith("CASE WHEN") and "CAST(" in sql
        assert "substr(" in sql  # A nonnumeric/large member cannot reach the result.
    sql = str(query.compile(dialect=dialect))
    assert "garmin_api_responses" not in sql
    assert "garmin_daily_projections" not in sql
    assert "garmin_record_observations.observed_at DESC" in sql
    assert "garmin_record_observations.id DESC" in sql


async def test_query_and_python_memory_do_not_grow_with_samples_or_versions(sessions, monkeypatch):
    engine = sessions.kw["bind"].sync_engine
    loads = json.loads
    for days, samples, versions in ((1, 2, 1), (92, 2, 1), (92, 4096, 1), (92, 4096, 128)):
        last = DAY + timedelta(days=days - 1)
        # Append only: each round adds a current revision, with many older versions
        # on one key. Only current, projected kind/day inputs may reach the reader.
        expected = []
        async with sessions.begin() as session:
            for offset in range(days):
                day = DAY + timedelta(days=offset)
                bodies = {
                    "daily-summaries": daily_summary(day),
                    "heart-rate": heart_rate(day),
                    "sleep": sleep(day),
                    "sleep-summaries": sleep(day),
                    "hrv": {"hrvSummary": {"lastNightAvg": 45}},
                    "hrv-summaries": {"lastNightAvg": 40},
                    "steps": {"totalSteps": 0, "totalDistance": -10.5},
                    "stress": {"values": {"overallStressLevel": -1}},
                    "spo2": {"averageSpO2": 97.5},
                }
                expected.append(reader.day_summary(str(day), bodies))
                expected[-1]["sleep_score"] = None
                rows = []
                for kind, body in bodies.items():
                    body.update(
                        heartRateValues=[[i, 70] for i in range(samples)],
                        stressValuesArray=[[i, 20] for i in range(samples)],
                        metadata={"unread": "x" * samples},
                    )
                    # Even an expected scalar may contain a huge malformed container.
                    if kind == "sleep":
                        body["sleepScores"]["overall"]["value"] = ["unread"] * samples
                    rows.append(
                        dict(
                            kind=kind,
                            key=str(day),
                            body=body,
                            body_sha256=f"{days}-{samples}-{versions}",
                            first_seen_at=NOW,
                        )
                    )
                await session.execute(insert(GarminRawRecord), rows)
            await session.execute(
                insert(GarminRawRecord),
                [
                    dict(
                        kind="steps",
                        key=str(DAY),
                        body={"totalSteps": i},
                        body_sha256=f"history-{days}-{samples}-{versions}-{i}",
                        first_seen_at=NOW,
                    )
                    for i in range(versions)
                ],
            )
        expected[0].update(steps=versions - 1, distance_m=None)
        async with sessions() as session:
            assert await reader.daily_summaries(session, DAY, last) == expected  # Warm caches.
            statements, decoded = [], []

            def capture(
                conn, cursor, statement, parameters, context, executemany, _saved=statements
            ):
                assert statement.lstrip().startswith("SELECT")
                _saved.append(statement)

            def scalar_only(value, *args, _saved=decoded, **kwargs):
                assert len(value) < 128
                parsed = loads(value, *args, **kwargs)
                assert not isinstance(parsed, dict | list)
                _saved.append(value)
                return parsed

            def no_body_decoder(*args):
                pytest.fail("Full raw JSON decoder used by the daily API")

            with monkeypatch.context() as patch:
                patch.setattr(reader.json, "loads", scalar_only)
                patch.setattr(
                    GarminRawRecord.body.type, "_cached_result_processor", no_body_decoder
                )
                event.listen(engine, "before_cursor_execute", capture)
                gc.collect()
                tracemalloc.start()
                try:
                    actual = await reader.daily_summaries(session, DAY, last)
                    retained, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                    event.remove(engine, "before_cursor_execute", capture)
            assert actual == expected and len(actual) == days
            assert len(statements) == 1 and len(decoded) <= days * 40
            assert peak < 2 * 1024 * 1024
            assert not session.identity_map and not session.new and not session.dirty
            print(f"{days=} {samples=} {versions=}: {retained=} {peak=}, queries=1")


@pytest.mark.parametrize("failure", [None, "parse", "fetch", "cancel"])
async def test_stream_closes_without_writing_or_ending_the_callers_transaction(
    sessions, monkeypatch, failure
):
    async with sessions.begin() as session:
        session.add_all(
            raw("steps", str(DAY + timedelta(days=i)), {"totalSteps": i}) for i in range(3)
        )
    async with sessions.begin() as session:
        transaction, stream = session.get_transaction(), session.stream
        before = (await session.execute(select(GarminRawRecord.__table__))).all()
        opened, seen, paused = [], 0, asyncio.Event()
        parse = reader._record

        async def tracked(*args, **kwargs):
            rows = await stream(*args, **kwargs)
            opened.append(rows)
            monkeypatch.setattr(rows, "close", AsyncMock(wraps=rows.close))
            next_row = type(rows).__anext__

            async def next_or_fail(self):
                if seen == 1 and failure == "fetch":
                    raise ValueError("synthetic fetch")
                if seen == 1 and failure == "cancel":
                    paused.set()
                    await asyncio.Event().wait()
                return await next_row(self)

            monkeypatch.setattr(type(rows), "__anext__", next_or_fail)
            return rows

        def parse_or_fail(*args):
            nonlocal seen
            seen += 1
            if failure == "parse":
                raise ValueError("synthetic parse")
            return parse(*args)

        monkeypatch.setattr(session, "stream", tracked)
        monkeypatch.setattr(reader, "_record", parse_or_fail)
        task = asyncio.create_task(reader.daily_summaries(session, DAY, DAY + timedelta(days=2)))
        try:
            if failure == "cancel":
                await asyncio.wait_for(paused.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif failure:
                with pytest.raises(ValueError, match="synthetic"):
                    await task
            else:
                assert len(await task) == 3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(opened) == 1 and opened[0].closed and opened[0].close.await_count == 1
        assert session.get_transaction() is transaction and transaction.is_active
        assert (await session.execute(select(GarminRawRecord.__table__))).all() == before
