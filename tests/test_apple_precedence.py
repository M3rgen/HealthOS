"""Apple values under the source precedence policy (dashboard days, heart chart, workouts).

Apple's rows carry the source code ``HEALTH_KIT`` with provider ``APPLE``: an Apple-only day
shows them; under the default health policy a Fitbit value comes first and Apple's stays its
alternative; Google's own HEALTH_KIT value (Apple data Google relays) and Apple's are both
kept, Google's first, never summed; under the activity policy a Garmin workout comes first,
and an Apple workout Google relays too is folded under Google's copy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from apple_norm_records import PHONE, hae_workout, quantity, sleep, stamp, workout
from identity_records import STEPS, fitbit, iso, phone, steps, store

from app.api.dashboard import place_workouts, workouts
from app.core.providers import APPLE, GARMIN, GOOGLE
from app.google_health.data_types import REGISTRY
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.identities.source_precedence import (
    PRIMARY,
    SECONDARY,
    Observation,
    Policy,
    resolve,
)
from app.services.providers.apple_normalize import normalize_records
from app.services.readers.dashboard_queries import DashboardQuery

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
APPLE_LABEL = "Apple Sağlık"


def utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 6, hour, minute, second, tzinfo=UTC)


async def normalize(sessions, *records):
    async with sessions.begin() as session:
        await normalize_records(session, list(records))


async def day_of(sessions, policy: Policy | None = None) -> dict:
    async with sessions() as session:
        data = await dashboard_data(
            session, query=DashboardQuery(end_date=DAY, days=1, timezone="UTC"), policy=policy
        )
    return data


async def test_an_apple_only_day_selects_health_kit(sessions):
    await normalize(
        sessions,
        quantity("StepCount", 4200, "count", utc(8), utc(9)),
        sleep("AsleepCore", utc(0), utc(6)),
        quantity("RestingHeartRate", 51, "count/min", utc(6)),
    )
    day = (await day_of(sessions))["days"][0]
    assert day["steps"] == 4200 and day["sources"]["steps"] == {
        "source": "HEALTH_KIT",
        "label": APPLE_LABEL,
        "alternatives": [],
    }
    assert day["sleep_seconds"] == 6 * 3600
    assert day["sleep_assumptions"] == ["from_normalized_columns"]
    assert day["resting_bpm"] == 51
    resolution = resolve("steps", [Observation("HEALTH_KIT", 4200, provider=APPLE)], Policy())
    assert resolution.selected is not None and resolution.selected.provider == APPLE


async def test_fitbit_comes_first_and_apple_stays_its_alternative(sessions, clock):
    clock.now = NOW
    await store(sessions, STEPS, steps(8000, utc(7), source=fitbit("Fitbit Air")))
    await normalize(sessions, quantity("StepCount", 7000, "count", utc(8), utc(9)))
    day = (await day_of(sessions))["days"][0]
    assert day["steps"] == 8000 and day["sources"]["steps"]["source"] == "FITBIT"
    assert day["sources"]["steps"]["alternatives"] == [
        {"source": "HEALTH_KIT", "label": APPLE_LABEL, "value": 7000.0}
    ]
    flipped = (await day_of(sessions, Policy(health=("HEALTH_KIT", "FITBIT"))))["days"][0]
    assert flipped["steps"] == 7000 and flipped["sources"]["steps"]["source"] == "HEALTH_KIT"


async def test_google_health_kit_and_apple_on_one_day_are_both_kept_not_summed(sessions, clock):
    clock.now = NOW
    await store(sessions, STEPS, steps(500, utc(7), source=phone()))
    await normalize(sessions, quantity("StepCount", 800, "count", utc(7), utc(8), source=PHONE))
    day = (await day_of(sessions))["days"][0]
    assert day["steps"] == 500  # Google's, never 1300
    assert day["sources"]["steps"] == {
        "source": "HEALTH_KIT",
        "label": APPLE_LABEL,
        "alternatives": [{"source": "HEALTH_KIT", "label": APPLE_LABEL, "value": 800.0}],
    }


async def test_heart_chart_adds_apple_only_in_minutes_google_health_kit_leaves_empty(
    sessions, clock
):
    clock.now = NOW
    relayed = {
        "dataSource": phone(),
        "heartRate": {"sampleTime": {"physicalTime": iso(utc(9))}, "beatsPerMinute": 70},
    }
    await store(sessions, REGISTRY["heart-rate"], relayed)
    await normalize(
        sessions,
        quantity("HeartRate", 90, "count/min", utc(9, 0, 30)),  # the minute Google covers
        quantity("HeartRate", 80, "count/min", utc(10)),
    )
    data = await day_of(sessions)
    chart = data["charts"]["heart_rate"]
    assert chart[9] == 70.0 and chart[10] == 80.0
    sources = data["charts"]["sources"]["heart_rate"]
    assert sources["source"] == "HEALTH_KIT" and sources["alternatives"] == []
    assert sources["minutes_by_source"] == {"HEALTH_KIT": 2}


def row(source_code: str, provider: str, identifier: str, start: datetime, end: datetime):
    return {
        "id": identifier,
        "exercise_type": "running",
        "start_time": start,
        "end_time": end,
        "duration_seconds": (end - start).total_seconds(),
        "calories_kcal": None,
        "distance_meters": None,
        "source_code": source_code,
        "provider": provider,
    }


def test_garmin_comes_first_for_an_overlapping_apple_workout():
    rows = place_workouts(
        [
            row(GARMIN, GARMIN, "g1", utc(17), utc(17, 40)),
            row("HEALTH_KIT", APPLE, "a1", utc(17, 2), utc(17, 38)),
        ],
        Policy(),
    )
    garmin, apple = rows
    assert garmin["priority"] == PRIMARY and apple["priority"] == SECONDARY
    assert apple["group"] == "g1" and [item["id"] for item in garmin["alternatives"]] == ["a1"]
    assert garmin["alternatives"][0]["source"] == APPLE_LABEL


def test_an_apple_workout_google_relays_is_folded_under_the_row_google_is_under():
    rows = place_workouts(
        [
            row(GARMIN, GARMIN, "g1", utc(17), utc(17, 40)),
            row("HEALTH_KIT", GOOGLE, "h1", utc(17), utc(17, 30)),
            row("HEALTH_KIT", APPLE, "a1", utc(17), utc(17, 31)),
            # Barely touching the Google copy (< half of the shorter): not the same workout.
            row("HEALTH_KIT", APPLE, "a2", utc(17, 25), utc(18, 30)),
        ],
        Policy(),
    )
    by_id = {item["id"]: item for item in rows}
    assert by_id["g1"]["priority"] == PRIMARY
    assert by_id["h1"]["priority"] == SECONDARY and by_id["h1"]["group"] == "g1"
    assert by_id["a1"]["priority"] == SECONDARY and by_id["a1"]["group"] == "g1"
    assert by_id["a2"]["priority"] == SECONDARY and by_id["a2"]["group"] == "g1"
    assert sorted(item["id"] for item in by_id["g1"]["alternatives"]) == ["a1", "a2", "h1"]
    assert all(by_id[key]["alternatives"] == [] for key in ("h1", "a1", "a2"))


async def test_workout_list_folds_the_relayed_copy_and_keeps_the_rest(sessions, clock):
    clock.now = NOW
    relayed = {
        "dataSource": phone(),
        "exercise": {
            "interval": {"startTime": iso(utc(17)), "endTime": iso(utc(17, 30))},
            "exerciseType": "RUNNING",
        },
    }
    await store(sessions, REGISTRY["exercise"], relayed)
    await normalize(
        sessions,
        workout("Running", utc(17), utc(17, 30), duration="30", durationUnit="min"),
        hae_workout({"id": "W-9", "name": "Yoga", "start": stamp(utc(19)), "end": stamp(utc(20))}),
    )
    async with sessions() as session:
        rows = await workouts(month=DAY.replace(day=1), timezone="UTC", session=session)
    by_type = {(item["provider"], item["exercise_type"]): item for item in rows}
    assert len(rows) == 3
    google = by_type[(GOOGLE, "RUNNING")]
    apple_run = by_type[(APPLE, "Running")]
    yoga = by_type[(APPLE, "Yoga")]
    assert google["priority"] == PRIMARY and google["source"] == APPLE_LABEL
    assert apple_run["priority"] == SECONDARY and apple_run["group"] == google["id"]
    assert [item["id"] for item in google["alternatives"]] == [apple_run["id"]]
    assert yoga["priority"] == PRIMARY and yoga["group"] is None and yoga["alternatives"] == []
    assert yoga["start_time"] == utc(19) and yoga["source_code"] == "HEALTH_KIT"
    assert yoga["end_time"] - yoga["start_time"] == timedelta(hours=1)
