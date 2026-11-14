"""Optional workout measurements retain publication, identity and archive boundaries."""

import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fit_factory import START as FIT_START
from fit_factory import activity as fit_activity
from garmin_records import activity, store_page
from sqlalchemy import DateTime, bindparam, event, select, text
from test_garmin_activity_ids import LONG_IDS, PREFIX, body
from test_garmin_activity_legacy_ids import legacy_page, legacy_row
from test_garmin_activity_results import EVIDENCE, START, STOP, normalize, snapshot
from test_garmin_normalize import DAY, NOW

from app.api.dashboard import router
from app.core.fit import FIT_EXERCISE
from app.core.normalization import payload_hash
from app.db.session import get_session
from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.models.health import ExerciseSession
from app.models.state import RawHealthData
from app.services.providers.fit_import import import_fit
from app.services.providers.fit_records import parse_fit
from app.services.providers.garmin_normalize import activity_row
from app.services.providers.garmin_readers import workouts

TABLES = (*EVIDENCE, ExerciseSession)
DURATIONS = [
    (-4, None),
    (25.125, 25.125),
    ("-1e-1000", None),
    (2401, None),
    ("2400.0000001", 2400),
    ("2400.0000000000000000001", 2400),
    (2400.999999, 2401),  # under one second over; the column rounds it to 2401.000
    ("2401.0000001", None),
    (0, 0),
    ("-0", 0),
    ("1e-1000", 0),
    (2400, 2400),
    (None, 2400),
    ("1e30", 2400),
    ("-1e30", 2400),
    (25.125, 25.125),
    (-4, None),
]


async def read_only(sessions):
    before = await snapshot(sessions, TABLES)
    async with sessions() as session:
        rows = await workouts(session, START, STOP)
    assert await snapshot(sessions, TABLES) == before
    return rows


async def api_duration(sessions, source="GARMIN"):
    """The ``source`` duration served by GET /api/dashboard/workouts."""
    application = FastAPI()
    application.include_router(router)

    async def dependency():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = dependency
    transport = httpx.ASGITransport(application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/dashboard/workouts", params={"month": "2026-11-01"})
    (item,) = [item for item in response.json() if item["source_code"] == source]
    return item["duration_seconds"]


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize(
    "duration,end,expected",
    [
        # Without endTimeGMT the end is derived and timedelta rounds it to microseconds.
        (2400.1240234375, None, 2400.124),  # end .124023: rounded down
        (3600.4169921875, None, 3600.417),  # end .416992: rounded down
        (1800.8482666015625, None, 1800.848),  # end .848267: rounded up
        (2400.0000006, None, 2400),  # end .000001: rounded up
        (3600.4169921875, "18:00:00", 3600.417),  # explicit end truncated to seconds
    ],
)
async def test_sub_second_overrun_keeps_duration(sessions, mode, duration, end, expected):
    record = {**activity(1, DAY), "duration": duration}
    if end:
        record["endTimeGMT"] = f"{DAY}T{end}Z"
    await store_page(sessions, "activities", [record], NOW)
    await normalize(sessions, mode)
    (row,) = await read_only(sessions)
    assert row["duration_seconds"] == await api_duration(sessions) == expected


async def test_negative_duration_without_explicit_end_stays_null(sessions):
    # Normalization drops raw activities with neither an end nor a positive duration.
    async with sessions.begin() as session:
        session.add(legacy_row(activity(1, DAY), duration_seconds=-4))
    (row,) = await read_only(sessions)
    assert row["duration_seconds"] is await api_duration(sessions) is None


@pytest.mark.parametrize("field", ["calories", "distance"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        {},
        [],
        "broken",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "1e30",
        "-1e30",
        "missing",
    ],
)
async def test_optional_invalid_measurements_keep_existing_null_policy(sessions, field, value):
    record = {**activity(1, DAY), field: value}
    if value == "missing":
        record.pop(field)
    await store_page(sessions, "activities", [record], NOW)
    await normalize(sessions, "direct")
    (row,) = await read_only(sessions)
    assert row["calories_kcal"] == (None if field == "calories" else 200)
    assert row["distance_meters"] == (None if field == "distance" else 3000)
    assert row["exercise_type"] == "walking"
    assert row["duration_seconds"] == 2400


@pytest.mark.parametrize("version", ["v1", "v2", "both"])
@pytest.mark.parametrize("value,expected", [(-2, None), (0, 0), ("-0", 0), (12.5, 12.5)])
async def test_normalized_only_rows_check_each_stored_sign_without_old_version_rescue(
    sessions, version, value, expected
):
    record = {**activity(1, DAY), "calories": value, "distance": value}
    async with sessions.begin() as session:
        if version in ("v1", "both"):
            session.add(legacy_row(record, "v1"))
        if version in ("v2", "both"):
            session.add(legacy_row(record))
        if version == "both":
            # Keep the old version positive, even when the visible v2 is negative.
            old = await session.scalar(
                select(ExerciseSession).where(
                    ExerciseSession.google_data_point_id == "garmin:activities:1"
                )
            )
            old.calories_kcal = old.distance_meters = 80
    (row,) = await read_only(sessions)
    assert row["calories_kcal"] == row["distance_meters"] == expected


@pytest.mark.parametrize("version", ["v1", "v2", "both"])
@pytest.mark.parametrize(
    "value,expected",
    [(-4, None), (2401, None), (0, 0), ("-0", 0), (25.125, 25.125), (2400, 2400), (None, 2400)],
)
async def test_normalized_only_duration_uses_stored_bounds(sessions, version, value, expected):
    record = {**activity(1, DAY), "endTimeGMT": f"{DAY}T17:40:00Z"}
    async with sessions.begin() as session:
        if version in ("v1", "both"):
            session.add(
                legacy_row(record, "v1", duration_seconds=25 if version == "both" else value)
            )
        if version in ("v2", "both"):
            session.add(legacy_row(record, duration_seconds=value))
    (row,) = await read_only(sessions)
    assert row["duration_seconds"] == expected
    assert row["calories_kcal"] == 200 and row["distance_meters"] == 3000


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("value", [None, "missing", "broken", True, "1e1000", "-1e1000"])
async def test_unstored_duration_retains_interval_fallback(sessions, mode, value):
    record = {**activity(1, DAY), "duration": value, "endTimeGMT": f"{DAY}T17:40:00Z"}
    if value == "missing":
        record.pop("duration")
    await store_page(sessions, "activities", [record], NOW)
    await normalize(sessions, mode)
    (row,) = await read_only(sessions)
    assert row["duration_seconds"] == 2400


@pytest.mark.parametrize(
    "value,expected",
    [(-4, None), (2401, None), (0, 0), ("-0", 0), (25.125, 25.125), (600, 600), (None, 600)],
)
async def test_fit_stored_duration_preserves_raw_eligibility(sessions, value, expected):
    async with sessions.begin() as session:
        await import_fit(session, parse_fit(fit_activity()))
        row = await session.scalar(select(ExerciseSession))
        row.duration_seconds = value
    before = await snapshot(sessions, (*TABLES, RawHealthData))
    async with sessions() as session:
        (row,) = await workouts(session, FIT_START, FIT_START + timedelta(hours=1))
    assert row["duration_seconds"] == expected
    assert await snapshot(sessions, (*TABLES, RawHealthData)) == before
    async with sessions.begin() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.data_type == FIT_EXERCISE.name)
        )
        raw.is_quarantined = True
    async with sessions() as session:
        assert await workouts(session, FIT_START, FIT_START + timedelta(hours=1)) == []


@pytest.mark.parametrize(
    "manufacturer,milliseconds,expected",
    [
        (32, 600000, 600),
        (32, 602999, 602.999),  # other manufacturers: under three seconds over
        (32, 603000, None),
        (32, 900000, None),
        (32, -1, None),
        (1, 600999, 600.999),  # Garmin FIT keeps the one-second rule
        (1, 601000, None),
    ],
)
async def test_fit_duration_tolerance_follows_the_manufacturer(
    sessions, manufacturer, milliseconds, expected
):
    async with sessions.begin() as session:
        fit = fit_activity(manufacturer=manufacturer, duration=max(milliseconds, 0))
        await import_fit(session, parse_fit(fit))
        if milliseconds < 0:  # FIT durations are unsigned: only a stored row can be negative
            row = await session.scalar(select(ExerciseSession))
            row.duration_seconds = milliseconds / 1000
    provider = "GARMIN" if manufacturer == 1 else "FIT"
    async with sessions() as session:
        (row,) = await workouts(session, FIT_START, FIT_START + timedelta(hours=1), provider)
    assert row["end_time"] - row["start_time"] == timedelta(seconds=600)
    assert row["duration_seconds"] == await api_duration(sessions, provider) == expected
    assert row["calories_kcal"] is not None and row["distance_meters"] == 2500


@pytest.mark.parametrize("field", ["calories", "duration"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("evidence", ["none", "verified", "mismatched"])
@pytest.mark.parametrize("numeric_id", [False, True])
async def test_legacy_sources_use_complete_ids_and_verified_result_links(
    sessions, monkeypatch, observed, evidence, numeric_id, field
):
    identifiers = (*LONG_IDS, PREFIX)
    if numeric_id:
        identifiers = tuple(map(int, identifiers))
    a, b, short = [body(identifier, endTimeGMT=f"{DAY}T17:40:00Z") for identifier in identifiers]
    negative = {**a, field: "-1e-1000"}
    # A and B share the old truncated raw key; the short id is exactly that prefix.
    records = [a, negative, b, short]
    if observed:
        for index, record in enumerate(records):
            await legacy_page(sessions, monkeypatch, [record], NOW + timedelta(minutes=index))
    else:
        async with sessions.begin() as session:
            for record in records:
                session.add(
                    GarminRawRecord(
                        kind="activities",
                        key=PREFIX,
                        body=record,
                        body_sha256=payload_hash(record),
                        first_seen_at=NOW,
                    )
                )
    async with sessions.begin() as session:
        session.add_all([legacy_row(record) for record in (a, b, short)])
        raw_ids = (
            await session.scalars(select(GarminRawRecord.id).order_by(GarminRawRecord.id))
        ).all()
        if evidence != "none":
            linked_id = identifiers[0 if evidence == "verified" else 1]
            session.add(
                GarminActivityResult(
                    source_record_id=f"activities:{PREFIX}",
                    raw_record_id=raw_ids[1],
                    normalized_key=f"garmin:activities:{linked_id}:v2",
                )
            )
    rows = await read_only(sessions)
    assert len(rows) == 3
    async with sessions() as session:
        ids = {
            str(row.id): row.source_record_id
            for row in await session.scalars(select(ExerciseSession))
        }
    api_field = "duration_seconds" if field == "duration" else "calories_kcal"
    values = {ids[row["id"]]: row[api_field] for row in rows}
    assert values == {
        f"activities:{identifiers[0]}": None,
        f"activities:{identifiers[1]}": 2400 if field == "duration" else 200,
        f"activities:{identifiers[2]}": 2400 if field == "duration" else 200,
    }
    assert all(row["distance_meters"] == 3000 for row in rows)
    if evidence == "verified":
        # A pending canonical-key observation cannot replace a published legacy input.
        await store_page(sessions, "activities", [a], NOW + timedelta(hours=1))
        assert await read_only(sessions) == rows


@pytest.mark.parametrize("size", [1, 64])
async def test_workouts_read_only_required_raw_members_with_one_bounded_query(
    sessions, monkeypatch, size
):
    records = [
        {
            **activity(index, DAY),
            "calories": "-1e-1000",
            "distance": "-1e-1000",
            "duration": "-1e-1000",
            "endTimeGMT": f"{DAY}T17:40:00Z",
            "unused": "x" * 32_768,
        }
        for index in range(size)
    ]
    await store_page(sessions, "activities", records, NOW)
    await normalize(sessions, "direct")
    before = await snapshot(sessions, TABLES)
    engine = sessions.kw["bind"].sync_engine
    statements = []
    streams = []

    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    async with sessions() as session:
        original = session.stream

        async def stream(statement, *args, **kwargs):
            assert statement.get_execution_options()["yield_per"] == 1000
            result = await original(statement, *args, **kwargs)
            streams.append(result)
            return result

        monkeypatch.setattr(session, "stream", stream)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            rows = await workouts(session, START, STOP)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == len(streams) == 1
    assert streams[0].closed
    projection = statements[0].split("FROM", 1)[0]
    assert projection.count("garmin_raw_records.body ->") == 3
    assert "garmin_api_responses" not in statements[0]
    assert "garmin_raw_records.body," not in projection
    assert len(rows) == size
    assert all(
        row["calories_kcal"] is row["distance_meters"] is row["duration_seconds"] is None
        for row in rows
    )
    assert await snapshot(sessions, TABLES) == before


async def test_other_providers_keep_their_numeric_policy(sessions):
    values = activity_row({**activity(1, DAY), "calories": -2, "distance": -3}, "Synthetic")
    async with sessions.begin() as session:
        session.add(ExerciseSession(**{**values, "provider": "DEMO"}))
    async with sessions() as session:
        (row,) = await workouts(session, START, STOP, provider="DEMO")
    assert row["calories_kcal"] == -2
    assert row["distance_meters"] == -3


@pytest.mark.parametrize("field", ["calories", "duration"])
@pytest.mark.parametrize(
    "token,expected,duration",
    [
        ("-1e-1000", None, None),
        ("1e-1000", 0, 0),
        ("-0e-1000", 0, 0),
        ("2400.0000000000000000001", 2400, 2400),
    ],
)
async def test_legacy_json_numeric_tokens_are_checked_before_float_decoding(
    sessions, token, expected, duration, field
):
    record = {
        **activity(1, DAY),
        "calories": token,
        "distance": token,
        "endTimeGMT": f"{DAY}T17:40:00Z",
        field: token,
    }
    # An old JSON archive may retain numeric precision beyond Python float. Insert
    # that original JSON text directly; ORM JSON encoding would turn it into a string.
    body_text = json.dumps(record).replace(json.dumps(token), token)
    async with sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO garmin_raw_records "
                "(kind, key, body, body_sha256, first_seen_at) "
                "VALUES ('activities', '1', :body, :digest, :seen)"
            ).bindparams(bindparam("seen", type_=DateTime(timezone=True))),
            {"body": body_text, "digest": payload_hash(record), "seen": NOW},
        )
        session.add(legacy_row(record))
    (row,) = await read_only(sessions)
    assert row["calories_kcal"] == row["distance_meters"] == expected
    if field == "duration":
        assert row["duration_seconds"] == duration
