"""Current Google exercise corrections through ingestion and the public workout API."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from derived_records import snapshot, store
from fastapi import FastAPI
from sqlalchemy import select
from support.workouts import METRIC_VALUES, OPTIONAL_METRICS, START, exercise

from app.api.dashboard import router
from app.db.session import get_session
from app.models.health import ExerciseSession
from app.models.identity_tables import RawHealthDataSupersession
from app.models.manual import ManualWorkout
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData

VALUES = ("exercise_type", "duration_seconds", "calories_kcal", "distance_meters")


@pytest.fixture
async def api(sessions):
    app = FastAPI()
    app.include_router(router)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


async def read(api, sessions, month="08", zone="UTC"):
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get(
        "/api/dashboard/workouts", params={"month": f"2026-{month}-01", "timezone": zone}
    )
    assert response.status_code == 200, response.text
    assert "NaN" not in response.text and "Infinity" not in response.text
    async with sessions() as session:
        assert await snapshot(session) == before
    rows = response.json()
    assert rows == sorted(rows, key=lambda row: row["start_time"], reverse=True)
    return rows


BAD_INTERVALS = [
    None,
    "broken",
    {},
    {"endTime": "2026-09-01T01:30:00Z"},
    {"startTime": "2026-09-01T00:30:00Z"},
]
BAD_TIMES = [None, "broken", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
BAD_NUMBERS = ["NaN", "Infinity", "-Infinity", "1e999999999999999999999"]
# Finite but wider than the optional SQL column: the correction is kept without it.
OPTIONAL_OVERFLOWS = [
    *((key, "1e1000000") for key in ("activeDuration", "caloriesKcal", "distanceMillimeters")),
    ("activeDuration", "99999999999.9995s"),
    *((key, value) for key in ("activeDuration", "duration") for value in ("-1e1000s", "1e1000s")),
    ("duration", "99999999999.9995s"),
    ("caloriesKcal", "9999999999.99995"),
    ("caloriesKcal", "1e20"),
    ("distanceMillimeters", "999999999999999.95"),
    ("distanceMillimeters", "1e30"),
]


@pytest.mark.parametrize(
    "field,value",
    [
        *(("interval", value) for value in BAD_INTERVALS),
        *((key, value) for key in ("startTime", "endTime") for value in BAD_TIMES),
        ("endTime", "2026-09-01T00:30:00Z"),
        ("endTime", "2026-08-31T23:30:00Z"),
        *(
            (key, value)
            for key in ("activeDuration", "duration", "caloriesKcal", "distanceMillimeters")
            for value in BAD_NUMBERS
        ),
    ],
)
async def test_invalid_correction_and_recovery(api, sessions, field, value):
    original, neighbor = exercise(), exercise("neighbor", exerciseType="WALKING")
    await store(sessions, "exercise", original, neighbor)
    async with sessions.begin() as session:
        session.add(
            ManualWorkout(
                exercise_type="Manuel",
                start_time=START,
                end_time=START + timedelta(hours=1),
                calories_kcal=0,
            )
        )
        raw_id = await session.scalar(
            select(RawHealthData.id).where(RawHealthData.google_data_point_id == original["name"])
        )
    initial = await read(api, sessions)
    identifier = next(row["id"] for row in initial if row["exercise_type"] == "RUNNING")
    invalid = exercise(start=datetime(2026, 9, 1, 0, 30, tzinfo=UTC), exerciseType="CYCLING")
    payload = invalid["exercise"]
    if field in {"startTime", "endTime"}:
        payload["interval"][field] = value
    elif field in {"caloriesKcal", "distanceMillimeters"}:
        payload["metricsSummary"][field] = value
    else:
        payload[field] = value
        if field == "duration":
            payload.pop("activeDuration")
    stats = await store(sessions, "exercise", invalid, neighbor)
    if stats.normalization_skipped:
        async with sessions() as session:
            retained = await session.get(ExerciseSession, UUID(identifier))
            assert tuple(getattr(retained, key) for key in VALUES) == ("RUNNING", 3500, 123, 4567)
    assert {row["id"] for row in await read(api, sessions)} == {
        row["id"] for row in initial if row["id"] != identifier
    }
    assert await read(api, sessions, "09") == []
    recovery = exercise(
        start=START + timedelta(hours=1),
        exerciseType="CYCLING",
        activeDuration=0,
        metricsSummary={"caloriesKcal": 0, "distanceMillimeters": 0},
    )
    recovery["exercise"]["interval"]["startTime"] = "2026-09-01T00:30:00+02:00"
    await store(sessions, "exercise", recovery)
    current = next(row for row in await read(api, sessions) if row["id"] == identifier)
    assert tuple(current[key] for key in VALUES) == ("CYCLING", 0, 0, 0)
    assert current["start_time"] == "2026-08-31T22:30:00Z"
    assert await read(api, sessions, "09") == []
    assert [row["id"] for row in await read(api, sessions, "09", "Europe/Paris")] == [identifier]
    assert len(await read(api, sessions, "08", "Europe/Paris")) == 2
    async with sessions() as session:
        raw = await session.get(RawHealthData, raw_id)
        assert raw.raw_json == recovery and raw.google_data_point_id == original["name"]
        versions = (await session.scalars(select(RawHealthDataVersion).order_by("version"))).all()
        assert [(row.version, row.raw_json) for row in versions] == [(1, original), (2, invalid)]


@pytest.mark.parametrize("field,value", OPTIONAL_OVERFLOWS)
async def test_optional_metric_overflow_keeps_the_correction_visible(api, sessions, field, value):
    await store(sessions, "exercise", exercise())
    corrected = exercise(exerciseType="CYCLING")
    payload = corrected["exercise"]
    (payload if field in {"activeDuration", "duration"} else payload["metricsSummary"])[field] = (
        value
    )
    if field == "duration":
        payload.pop("activeDuration")
    stats = await store(sessions, "exercise", corrected)
    assert (stats.normalized, stats.normalization_skipped) == (1, 0)
    [row] = await read(api, sessions)
    expected = dict(zip(VALUES, ("CYCLING", 3500, 123, 4567), strict=True))
    if field in {"activeDuration", "duration"}:
        expected["duration_seconds"] = 3600  # An unstorable duration shows the interval.
    else:
        expected["calories_kcal" if field == "caloriesKcal" else "distance_meters"] = None
    assert {key: row[key] for key in VALUES} == expected


@pytest.mark.parametrize("fields", [*((f,) for f in OPTIONAL_METRICS), tuple(OPTIONAL_METRICS)])
@pytest.mark.parametrize("value", METRIC_VALUES)
@pytest.mark.parametrize("corrected", [False, True], ids=["new", "correction"])
async def test_optional_metric_sign_and_recovery(api, sessions, fields, value, corrected):
    original = exercise()
    history = []
    if corrected:
        await store(sessions, "exercise", original)
        history.append(original)
    previous = None
    for selected in dict.fromkeys((value, "-0", "2345.5")):
        metrics = {**original["exercise"]["metricsSummary"], **dict.fromkeys(fields, selected)}
        current = exercise(metricsSummary=metrics)
        stats = await store(sessions, "exercise", current)
        assert stats.normalization_skipped == 0
        [row] = await read(api, sessions)
        expected = {
            column: None if Decimal(str(metrics[field])) < 0 else float(metrics[field]) / divisor
            for (field, column), divisor in zip(OPTIONAL_METRICS.items(), (1, 1000), strict=True)
        }
        assert {column: row[column] for column in OPTIONAL_METRICS.values()} == expected
        if previous is not None:
            assert row == {**previous, **expected}
        assert row["exercise_type"] == "RUNNING" and row["duration_seconds"] == 3500
        assert row["source_code"] == "FITBIT" and row["provider"] == "GOOGLE"
        assert row["start_time"] == "2026-08-31T21:30:00Z"
        assert row["end_time"] == "2026-08-31T22:30:00Z"
        async with sessions() as session:
            raw = await session.scalar(select(RawHealthData))
            assert raw.raw_json == current
            versions = (
                await session.scalars(select(RawHealthDataVersion).order_by("version"))
            ).all()
            assert [(v.version, v.raw_json) for v in versions] == list(enumerate(history, 1))
            normalized = await session.get(ExerciseSession, UUID(row["id"]))
            # The read projection must not clear the stored negative/rounded measurement.
            for field in fields:
                stored = getattr(normalized, OPTIONAL_METRICS[field])
                if Decimal(str(selected)) < 0:
                    assert stored is None or stored <= 0
        previous = row
        if not history or current != history[-1]:
            history.append(current)


@pytest.mark.parametrize("field", ["activeDuration", "duration"])
@pytest.mark.parametrize("corrected", [False, True], ids=["new", "correction"])
@pytest.mark.parametrize("value", [-60, -0.0001, 7200, 3600.0001, 3600.001, -0.0, 0, 3500, 3600])
@pytest.mark.parametrize("form", [float, str, "{}s".format], ids=["number", "text", "seconds"])
async def test_selected_duration_bounds_and_recovery(api, sessions, field, corrected, value, form):
    original = exercise()
    if corrected:
        await store(sessions, "exercise", original)
        [initial] = await read(api, sessions)
    current = exercise(**{field: form(value)})
    if field == "duration":
        current["exercise"].pop("activeDuration")
    stats = await store(sessions, "exercise", current)
    assert (stats.normalized, stats.normalization_skipped) == (
        int(not corrected or current != original),
        0,
    )
    [row] = await read(api, sessions)
    expected = value if 0 <= value <= 3600 else None
    assert row["duration_seconds"] == expected
    if corrected:
        assert row == {**initial, "duration_seconds": expected}
    assert tuple(row[key] for key in ("exercise_type", "calories_kcal", "distance_meters")) == (
        "RUNNING",
        123,
        4567,
    )
    assert row["start_time"] == "2026-08-31T21:30:00Z"
    assert row["end_time"] == "2026-08-31T22:30:00Z"
    assert row["source_code"] == "FITBIT" and row["provider"] == "GOOGLE"
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
        assert raw.raw_json == current
        normalized = await session.get(ExerciseSession, UUID(row["id"]))
        assert normalized.duration_seconds == Decimal(str(value)).quantize(Decimal("0.001"))
    recovery = exercise(activeDuration="0s")
    await store(sessions, "exercise", recovery)
    [recovered] = await read(api, sessions)
    assert recovered == {**row, "duration_seconds": 0}
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
        assert raw.raw_json == recovery
        versions = (await session.scalars(select(RawHealthDataVersion).order_by("version"))).all()
        # Identical refetches do not add archive versions.
        previous = ([original] if corrected and original != current else []) + (
            [current] if current != recovery else []
        )
        assert [(v.version, v.raw_json) for v in versions] == list(enumerate(previous, 1))


@pytest.mark.parametrize("field", ["activeDuration", "duration"])
@pytest.mark.parametrize("corrected", [False, True], ids=["new", "correction"])
@pytest.mark.parametrize(
    "boundary,above",
    [
        ("3600", "3600.0000000000000001"),
        ("0.3", "0.30000000000000001"),
        ("0.000001", "0.00000100000000000001"),
        ("0.0006", "0.00060000000000000001"),
        ("86400.000001", "86400.000001000000001"),
        ("90000000000.000001", "90000000000.0000010001"),
    ],
)
@pytest.mark.parametrize("selected", ["boundary", "above", "-1e-1000s", "-0s", "-0e-1000s"])
async def test_duration_source_precision_and_recovery(
    api, sessions, field, corrected, boundary, above, selected
):
    whole, _, fraction = boundary.partition(".")
    interval = {
        "startTime": START.isoformat(),
        "endTime": (
            START + timedelta(seconds=int(whole), microseconds=int(fraction.ljust(6, "0")))
        ).isoformat(),
    }
    original = exercise(interval=interval, activeDuration="0s")
    history = []
    initial = None
    if corrected:
        await store(sessions, "exercise", original)
        [initial] = await read(api, sessions)
        history.append(original)
    source = {"boundary": boundary + "s", "above": above + "s"}.get(selected, selected)
    current = exercise(interval=interval, **{field: source})
    if field == "duration":
        current["exercise"].pop("activeDuration")
    else:
        current["exercise"]["duration"] = "0s"  # A valid alternative cannot rescue it.
    previous = initial
    for record in (current, original):
        stats = await store(sessions, "exercise", record)
        assert stats.normalization_skipped == 0
        [row] = await read(api, sessions)  # Snapshots prove every read leaves all tables intact.
        stored = Decimal(source[:-1]).quantize(Decimal("0.001")) if record == current else 0
        expected = (
            None if record == current and selected in {"above", "-1e-1000s"} else float(stored)
        )
        assert row["duration_seconds"] == expected
        if previous is not None:
            assert row == {**previous, "duration_seconds": expected}
        assert tuple(row[key] for key in ("exercise_type", "calories_kcal", "distance_meters")) == (
            "RUNNING",
            123,
            4567,
        )
        assert row["source_code"] == "FITBIT" and row["provider"] == "GOOGLE"
        async with sessions() as session:
            raw = await session.scalar(select(RawHealthData))
            assert raw.raw_json == record
            normalized = await session.get(ExerciseSession, UUID(row["id"]))
            assert normalized.duration_seconds == stored
            versions = (
                await session.scalars(select(RawHealthDataVersion).order_by("version"))
            ).all()
            assert [v.raw_json for v in versions] == history
        previous = row
        history.append(record)


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"activeDuration": None}, ("RUNNING", 3600, 123, 4567)),
        ({"activeDuration": None, "duration": None}, ("RUNNING", 3600, 123, 4567)),
        *(
            ({"activeDuration": value, "duration": "1.2345s"}, ("RUNNING", 1.235, 123, 4567))
            for value in (None, "broken", {}, [], True)
        ),
        *(
            ({"activeDuration": "broken", "duration": value}, ("RUNNING", None, 123, 4567))
            for value in ("-0.0001s", "-1e-1000s", "3600.0001s")
        ),
        ({"activeDuration": "-0s", "duration": "-1s"}, ("RUNNING", 0, 123, 4567)),
        ({"activeDuration": "1.2345s", "duration": "broken"}, ("RUNNING", 1.235, 123, 4567)),
        ({"activeDuration": "broken", "duration": "broken"}, ("RUNNING", 3600, 123, 4567)),
        (
            {"exerciseType": "", "displayName": "Walk", "metricsSummary": {}},
            ("Walk", 3500, None, None),
        ),
        ({"exerciseType": None, "displayName": ""}, ("", 3500, 123, 4567)),
        *(
            ({"exerciseType": value, "displayName": "Walk"}, (None, 3500, 123, 4567))
            for value in ({"bad": 1}, ["bad"], 42, True, "ç" * 129)
        ),
        ({"exerciseType": False, "displayName": "Walk"}, ("Walk", 3500, 123, 4567)),
        ({"exerciseType": None, "displayName": False}, (None, 3500, 123, 4567)),
        ({"metricsSummary": "broken"}, ("RUNNING", 3500, None, None)),
    ],
)
async def test_current_field_priorities_and_optional_text(api, sessions, fields, expected):
    await store(sessions, "exercise", exercise())
    await store(sessions, "exercise", exercise(**fields))
    [row] = await read(api, sessions)
    assert tuple(row[key] for key in VALUES) == expected


@pytest.mark.parametrize("active", [False, True])
async def test_identity_link_after_correction(api, sessions, active):
    original, holder = exercise(), exercise("holder")
    await store(sessions, "exercise", original, holder)
    await store(sessions, "exercise", exercise(exerciseType="CYCLING"))
    async with sessions.begin() as session:
        raws = {
            row.google_data_point_id: row for row in await session.scalars(select(RawHealthData))
        }
        old, new = raws[original["name"]], raws[holder["name"]]
        session.add(
            RawHealthDataSupersession(
                old_raw_id=old.id,
                new_raw_id=new.id,
                data_type="exercise",
                old_google_data_point_id=old.google_data_point_id,
                new_google_data_point_id=new.google_data_point_id,
                reason="duplicate",
                old_fingerprint="a" * 64,
                new_fingerprint=("a" if active else "b") * 64,
                same_reading=True,
                new_payload_hash=new.payload_hash,
            )
        )
    rows = await read(api, sessions)
    assert [row["exercise_type"] for row in rows].count("CYCLING") == (not active)
    assert [row["exercise_type"] for row in rows].count("RUNNING") == 1
