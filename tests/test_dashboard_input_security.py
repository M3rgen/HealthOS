"""Authenticated manual inputs fail safely before reaching persistence."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from support.auth import TOKEN, build

from app.db.session import get_session
from app.models.manual import ManualMeasurement, ManualWorkout

WORKOUT = {
    "exercise_type": " Yürüyüş ",
    "start_time": "2026-01-01T10:00:00+02:00",
    "end_time": "2026-01-01T11:00:00+02:00",
    "calories_kcal": "125.5",
    "distance_meters": "2000",
}
MEASUREMENT = {
    "metric": "weight",
    "value": "75.5",
    "measured_at": "2026-01-01T10:00:00+02:00",
}


@pytest.fixture
async def client(sessions, monkeypatch):
    application = build(monkeypatch)

    async def session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://healthos.localhost:8000"
    ) as client:
        assert (await client.post("/login", data={"token": TOKEN})).status_code == 303
        client.headers["X-HealthOS-Request"] = "1"
        yield client


@pytest.mark.parametrize(
    "path,body,field,bad",
    [
        ("workouts", WORKOUT, "exercise_type", '"Yürüyüş\\u0000"'),
        ("workouts", WORKOUT, "exercise_type", '"\\ud800"'),
        *[
            (path, body, field, json.dumps(value))
            for path, body, field in [
                ("workouts", WORKOUT, "start_time"),
                ("workouts", WORKOUT, "end_time"),
                ("measurements", MEASUREMENT, "measured_at"),
            ]
            for value in ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"]
        ],
        *[
            (path, body, field, value)
            for path, body, field in [
                ("workouts", WORKOUT, "calories_kcal"),
                ("workouts", WORKOUT, "distance_meters"),
                ("measurements", MEASUREMENT, "value"),
            ]
            for value in [
                "1e999999",
                "-1e999999",
                '"1e999999"',
                "NaN",
                '"1e-999999"',
                '"0e999999"',
                '"0e-999999"',
            ]
        ],
    ],
)
async def test_invalid_input_is_fixed_turkish_422_and_writes_nothing(
    client, sessions, path, body, field, bad
):
    encoded = json.dumps({**body, field: "REPLACE"}).replace('"REPLACE"', bad)
    response = await client.post(
        "/api/dashboard/" + path, content=encoded, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Girdi geçersiz. Lütfen değerleri kontrol edin."}
    async with sessions() as session:
        for model in (ManualMeasurement, ManualWorkout):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_valid_offset_inputs_remain_usable_and_utc(client):
    workout = await client.post("/api/dashboard/workouts", json=WORKOUT)
    measurement = await client.post("/api/dashboard/measurements", json=MEASUREMENT)
    assert workout.status_code == measurement.status_code == 201
    assert workout.json()["exercise_type"] == "Yürüyüş"
    assert workout.json()["start_time"] == "2026-01-01T08:00:00Z"
    assert workout.json()["duration_seconds"] == 3600
    assert workout.json()["calories_kcal"] == 125.5
    measured = datetime.fromisoformat(measurement.json()["measured_at"])
    assert measured.replace(tzinfo=UTC) == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert float(measurement.json()["value"]) == 75.5
    assert len((await client.get("/api/dashboard/measurements")).json()) == 1


async def test_rejected_input_does_not_poison_following_write(client):
    rejected = await client.post(
        "/api/dashboard/workouts", json={**WORKOUT, "exercise_type": "bad\x00type"}
    )
    assert rejected.status_code == 422
    assert (await client.post("/api/dashboard/workouts", json=WORKOUT)).status_code == 201


@pytest.mark.parametrize("reason", ["bad\x00reason", "\ud800"])
async def test_flag_text_uses_same_validation_boundary(client, reason):
    response = await client.post(
        "/api/dashboard/measurements/00000000-0000-0000-0000-000000000001/flag",
        content=json.dumps({"reason": reason}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Girdi geçersiz. Lütfen değerleri kontrol edin."}


async def test_representable_early_date_and_small_decimal_still_work(client):
    response = await client.post(
        "/api/dashboard/workouts",
        json={
            **WORKOUT,
            "start_time": "0001-01-01T00:00:00Z",
            "end_time": "0001-01-01T01:00:00Z",
            "calories_kcal": "1e-10",
        },
    )
    assert response.status_code == 201
    assert response.json()["start_time"] == "0001-01-01T00:00:00Z"
    assert response.json()["calories_kcal"] == 0
