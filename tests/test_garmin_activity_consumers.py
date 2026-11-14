"""Sports, source-priority heart windows and Umay agree on completed activity results."""

from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from garmin_records import FakeGarmin, activity, store_page
from identity_records import HEART, fitbit, heart, store
from sqlalchemy import true
from test_garmin_activity_results import EVIDENCE, START, STOP, normalize, snapshot
from test_garmin_normalize import DAY, NOW, garmin_heart
from test_garmin_pull import TODAY, Clock, service
from test_garmin_workout_validity import DURATIONS

from app.api.dashboard import router, workouts
from app.db.session import get_session
from app.google_health.data_types import REGISTRY
from app.models.garmin import GarminActivityResult
from app.models.health import ExerciseSession
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.providers.garmin_readers import workouts as provider_workouts
from app.services.providers.ingestion_readers import GARMIN_ARCHIVE
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.umay.codex_health import read_health


async def test_sports_api_heart_windows_and_umay_exclude_invalidated_activity(sessions, clock):
    minute = START + timedelta(hours=17, minutes=10)
    await store(sessions, HEART, heart(100, minute, name="Fitbit Air"))
    await store(
        sessions,
        REGISTRY["exercise"],
        {
            "dataSource": fitbit("Fitbit Air"),
            "exercise": {
                "interval": {
                    "startTime": minute.isoformat(),
                    "endTime": (minute + timedelta(minutes=20)).isoformat(),
                },
                "exerciseType": "WALKING",
            },
        },
    )
    await store_page(sessions, "heart-rate", [garmin_heart(DAY, [(17 * 60 + 10, 120)])], NOW)
    a = activity(1, DAY)
    for index, payload in enumerate((a, {**a, "duration": 0}, a)):
        await store_page(sessions, "activities", [payload], NOW + timedelta(minutes=index))
        await normalize(sessions, "direct")
        valid = index != 1
        async with sessions() as session:
            assert bool(await provider_workouts(session, START, STOP)) == valid
            samples = await GARMIN_ARCHIVE.heart_rate(session, START, STOP, true())
            assert bool(samples.activity_windows) == valid
            rows = await workouts(month=DAY.replace(day=1), timezone="UTC", session=session)
            assert {row["source_code"] for row in rows} == (
                {"FITBIT", "GARMIN"} if valid else {"FITBIT"}
            )
            fitbit_row = next(row for row in rows if row["source_code"] == "FITBIT")
            assert fitbit_row["priority"] == ("secondary" if valid else "primary")
            dashboard = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"))
        umay = await read_health(
            sessions,
            {
                "action": "daily",
                "start_date": str(DAY),
                "end_date": str(DAY),
                "timezone": "UTC",
            },
        )
        assert (
            umay["charts"]["heart_rate"][17]
            == dashboard["charts"]["heart_rate"][17]
            == (120 if valid else 100)
        )
        assert dashboard["charts"]["sources"]["heart_rate"]["activity_windows"] == (
            "GARMIN" if valid else None
        )


@pytest.mark.parametrize("status", [200, 204, 503])
async def test_empty_or_failed_activity_pull_is_not_a_source_deletion(sessions, tmp_path, status):
    key = tmp_path / "key"
    key.write_text("synthetic")
    fake = FakeGarmin()
    path = "/activitylist-service/activities/search/activities"
    fake.bodies[path] = [activity(1, TODAY)]
    pull = service(sessions, fake, key, Clock())
    try:
        assert not (await pull.run(["activities"])).failed
        before = await snapshot(sessions, (GarminActivityResult,))
        fake.bodies[path] = []
        fake.statuses[path] = [status] * 10
        result = await pull.run(["activities"])
        assert result.failed == (status == 503)
        assert await snapshot(sessions, (GarminActivityResult,)) == before
        async with sessions() as session:
            assert len(await provider_workouts(session, START, STOP + timedelta(days=7))) == 1
    finally:
        await pull.aclose()


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize(
    "field,api_field",
    [
        ("calories", "calories_kcal"),
        ("distance", "distance_meters"),
        ("duration", "duration_seconds"),
    ],
)
async def test_workout_signs_follow_published_inputs_in_provider_and_sports_api(
    sessions, clock, mode, field, api_field
):
    await store(
        sessions,
        REGISTRY["exercise"],
        {
            "dataSource": fitbit("Synthetic"),
            "exercise": {
                "interval": {
                    "startTime": (START + timedelta(hours=17)).isoformat(),
                    "endTime": (START + timedelta(hours=18)).isoformat(),
                },
                "exerciseType": "WALKING",
            },
        },
    )
    await store_page(sessions, "heart-rate", [garmin_heart(DAY, [(17 * 60 + 10, 120)])], NOW)
    application = FastAPI()
    application.include_router(router)

    async def dependency():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = dependency
    previous = original_id = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        # Starts negative; then corrects the same id, including a return to an old raw version.
        for index, (value, expected) in enumerate(
            DURATIONS
            if field == "duration"
            else [
                (-4, None),
                (25, 25),
                (-0.00000001, None),
                ("-1e-8", None),
                ("-1e-1000", None),
                (0, 0),
                (-0.0, 0),
                ("-0e-1000", 0),
                ("1e-1000", 0),
                (25, 25),
                (-4, None),
            ]
        ):
            await store_page(
                sessions,
                "activities",
                [{**activity(1, DAY), field: value, "endTimeGMT": f"{DAY}T17:40:00Z"}],
                NOW + timedelta(minutes=index),
            )
            before = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
            async with sessions() as session:
                pending = await provider_workouts(session, START, STOP)
                assert (pending[0][api_field] if pending else None) == previous
                assert len(pending) == bool(index)
            pending_api = await client.get(
                "/api/dashboard/workouts", params={"month": str(DAY.replace(day=1))}
            )
            assert pending_api.status_code == 200
            published = [item for item in pending_api.json() if item["source_code"] == "GARMIN"]
            assert len(published) == bool(index)
            if published:
                assert published[0][api_field] == previous
            assert await snapshot(sessions, (*EVIDENCE, ExerciseSession)) == before
            await normalize(sessions, mode)
            before = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
            async with sessions() as session:
                (row,) = await provider_workouts(session, START, STOP)
            response = await client.get(
                "/api/dashboard/workouts", params={"month": str(DAY.replace(day=1))}
            )
            assert response.status_code == 200
            sports = response.json()
            garmin = next(item for item in sports if item["source_code"] == "GARMIN")
            other = "distance_meters" if field == "calories" else "calories_kcal"
            assert row[api_field] == garmin[api_field] == expected
            assert row[other] == garmin[other] == (3000 if field == "calories" else 200)
            if field != "duration":
                assert row["duration_seconds"] == garmin["duration_seconds"] == 2400
            else:
                assert row["distance_meters"] == garmin["distance_meters"] == 3000
            original_id = original_id or row["id"]
            assert row["id"] == garmin["id"] == original_id
            assert row["end_time"] == START + timedelta(hours=17, minutes=40)
            async with sessions() as session:
                heart = await GARMIN_ARCHIVE.heart_rate(session, START, STOP, true())
                assert heart.activity_windows == [(row["start_time"], row["end_time"])]
            assert row["start_time"] == START + timedelta(hours=17)
            assert garmin["priority"] == "primary"
            assert (
                next(item for item in sports if item["source_code"] == "FITBIT")["priority"]
                == "secondary"
            )
            assert await snapshot(sessions, (*EVIDENCE, ExerciseSession)) == before
            previous = expected
    # Returning to the first payload reuses its immutable input, with a new result.
    evidence = await snapshot(sessions, (GarminActivityResult,))
    results = evidence[GarminActivityResult]
    assert results[-1].raw_record_id == results[0].raw_record_id
