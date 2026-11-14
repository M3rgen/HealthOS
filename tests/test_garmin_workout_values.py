"""Seeded multiset equivalence with the independent pre-733 workout query."""

from collections import Counter
from datetime import timedelta
from random import Random

import pytest
from asyncpg.exceptions import CardinalityViolationError
from garmin_records import activity, store_page
from garmin_workout_reference import reference_query
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError
from support.postgres_reads import read_statements
from test_garmin_activity_ids import LONG_IDS, PREFIX, body
from test_garmin_activity_legacy_ids import legacy_page, legacy_row
from test_garmin_activity_results import START, STOP, normalize
from test_garmin_normalize import DAY, NOW

from app.core.normalization import payload_hash
from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.services.providers.garmin_workout_values import _duration, _measurement, workout_values


async def seeded_archive(sessions, monkeypatch, seed):
    random = Random(seed)
    records = []
    for i in range(32):
        records.append(
            {
                **activity(i, DAY),
                "startTimeGMT": f"{DAY}T{(i % 12):02d}:00:00Z",
                "endTimeGMT": f"{DAY}T{(i % 12):02d}:40:00Z",
                "calories": random.choice([0, 40, -2, "-1e-1000"]),
                "distance": random.choice([0, 3000, -3, None]),
                "duration": random.choice([None, 2400.999999, -1, 2401, "2400.0000001"]),
            }
        )
    await store_page(sessions, "activities", records, NOW)
    await normalize(sessions, "direct")
    # Invalid completion, changed input, A -> B -> A, and an unpublished observation.
    for minute in (1, 2):
        changed = [
            {**r, "startTimeGMT": None}
            if i % 4 == 0
            else {**r, "calories": 80}
            if minute == 1
            else r
            for i, r in enumerate(records)
            if i % 4 != 3
        ]
        await store_page(sessions, "activities", changed, NOW + timedelta(minutes=minute))
        await normalize(sessions, "direct")
    await store_page(
        sessions, "activities", [{**r, "calories": -8} for r in records], NOW + timedelta(minutes=3)
    )
    # Long identities share a legacy raw prefix; a real short id equals that prefix.
    legacy = [body(identifier, endTimeGMT=f"{DAY}T17:40:00Z") for identifier in (*LONG_IDS, PREFIX)]
    await legacy_page(sessions, monkeypatch, legacy)
    async with sessions.begin() as session:
        for record in legacy:
            session.add_all([legacy_row(record, "v1"), legacy_row(record)])
        raw = await session.scalar(
            select(GarminRawRecord)
            .where(GarminRawRecord.key == PREFIX)
            .order_by(GarminRawRecord.id)
        )
        session.add(
            GarminActivityResult(
                source_record_id=f"activities:{PREFIX}",
                raw_record_id=raw.id,
                normalized_key=f"garmin:activities:{LONG_IDS[0]}:v2",
            )
        )
        # Normalized-only rows, quarantines, other providers and both query boundaries.
        for i in range(40, 48):
            record = activity(i, DAY)
            row = legacy_row(record, "v1" if i % 2 else "v2")
            row.provider = "DEMO" if i % 3 == 0 else "GARMIN"
            row.calories_kcal = random.choice([-1, 0, 200])
            row.duration_seconds = random.choice([-1, 2401, 2400, None])
            row.is_quarantined = i == 40
            if i in (41, 42):
                row.start_time = START - timedelta(seconds=1) if i == 41 else STOP
                row.end_time = row.start_time + timedelta(seconds=2400)
            session.add(row)


@pytest.mark.parametrize("seed", [7, 73, 733])
@pytest.mark.parametrize("provider", ["GARMIN", "DEMO"])
async def test_workout_sql_and_values_match_reference(sessions, monkeypatch, seed, provider):
    await seeded_archive(sessions, monkeypatch, seed)
    statements, queries = [], []
    engine = sessions.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    async with sessions() as session:
        original = session.stream

        async def stream(statement, **kwargs):
            queries.append(statement.execution_options(yield_per=None, stream_results=False))
            return await original(statement, **kwargs)

        monkeypatch.setattr(session, "stream", stream)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            actual = [value async for value in workout_values(session, START, STOP, provider)]
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert len(read_statements(statements)) == len(queries) == 1
        old = (await session.execute(reference_query(START, STOP, provider))).all()
        new = (await session.execute(queries[0])).all()
        assert Counter((r.id, a, b, c) for r, a, b, c in new) == Counter(
            (r.id, a, b, c) for r, a, b, c in old
        )
        expected = Counter(
            (
                r.id,
                _measurement(r.calories_kcal, a, provider),
                _measurement(r.distance_meters, b, provider),
                _duration(r, c, provider),
            )
            for r, a, b, c in old
        )
        assert Counter((r.id, a, b, c) for r, a, b, c in actual) == expected
        times = [row.start_time for row, *_ in actual]
        assert times == sorted(times)


@pytest.mark.parametrize("alias", ["shared", "different"])
@pytest.mark.parametrize("provider", ["GARMIN", "DEMO"])
async def test_ambiguous_legacy_inputs_keep_the_reference_scalar_behavior(
    sessions, alias, provider
):
    # Historical inconsistent links are not repaired by choosing max(raw_id).
    async with sessions.begin() as session:
        for identifier in (1, 2):
            record = {**activity(identifier, DAY), "calories": -1}
            session.add(
                GarminRawRecord(
                    kind="activities",
                    key="shared" if alias == "shared" else str(identifier),
                    body=record,
                    body_sha256=payload_hash(record),
                    first_seen_at=NOW,
                )
            )
        session.add(
            legacy_row(
                activity(1, DAY),
                provider=provider,
                source_record_id="activities:shared" if alias == "shared" else "activities:1",
                google_data_point_id="garmin:activities:2:v2",
            )
        )

    async def read(reference):
        async with sessions() as session:
            try:
                if not reference:
                    return [
                        (r.id, a, b, c)
                        async for r, a, b, c in workout_values(session, START, STOP, provider)
                    ]
                rows = (await session.execute(reference_query(START, STOP, provider))).all()
                return [
                    (
                        r.id,
                        _measurement(r.calories_kcal, a, provider),
                        _measurement(r.distance_meters, b, provider),
                        _duration(r, c, provider),
                    )
                    for r, a, b, c in rows
                ]
            except (DBAPIError, CardinalityViolationError) as error:
                assert "more than one row" in str(getattr(error, "orig", error))
                return "multiple raw inputs"

    assert await read(False) == await read(True)


@pytest.mark.parametrize(
    "source,key",
    [("garmin:activities:1", "normalized-only"), ("normalized-only", "activities:1")],
)
async def test_legacy_aliases_keep_source_and_normalized_namespaces_separate(sessions, source, key):
    await store_page(sessions, "activities", [{**activity(1, DAY), "calories": -1}], NOW)
    async with sessions.begin() as session:
        session.add(legacy_row(activity(2, DAY), source_record_id=source, google_data_point_id=key))
    async with sessions() as session:
        (old,) = (await session.execute(reference_query(START, STOP))).all()
        assert _measurement(old[0].calories_kcal, old[1], "GARMIN") == 200
        (actual,) = [v async for v in workout_values(session, START, STOP, "GARMIN")]
        assert actual[1] == 200
