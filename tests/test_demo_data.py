"""The synthetic sample data: deterministic, labelled, readable, and removable only by itself."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.core.providers import DEMO, DEMO_KEY_PREFIX, GARMIN
from app.models.health import ExerciseSession, HeartRate, SleepSession, Steps, Weight
from app.models.manual import ManualMeasurement
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.providers.demo_archive import (
    SampleRefused,
    load_sample,
    remove_sample,
    sample_status,
)
from app.services.providers.demo_data import DAYS, DEVICE, HEART_STEP, TABLES, generate
from app.services.readers.dashboard_queries import DashboardQuery

ZONE = ZoneInfo("Europe/Istanbul")
TODAY = date(2026, 11, 11)
# 14:30 in Istanbul: today's sleep and morning values exist, the evening does not yet.
NOW = datetime(2026, 11, 11, 11, 30, tzinfo=UTC)


def keyed(sample):
    return {
        model.__tablename__: {row["google_data_point_id"]: row for row in rows}
        for model, rows in sample.rows.items()
    }


def test_same_inputs_give_the_same_rows_and_another_seed_does_not():
    first, again = generate(TODAY, ZONE, NOW), generate(TODAY, ZONE, NOW)
    assert first.rows == again.rows
    assert generate(TODAY, ZONE, NOW, seed="another").rows != first.rows


def test_a_later_load_keeps_the_values_of_the_days_both_cover():
    earlier = keyed(generate(TODAY, ZONE, NOW))
    later = keyed(generate(TODAY + timedelta(days=3), ZONE, NOW + timedelta(days=3)))
    for table, rows in earlier.items():
        shared = rows.keys() & later[table].keys()
        assert shared, table
        # Today's steps were cut at NOW in the earlier load; every finished row is the same.
        for key in shared - {f"{DEMO_KEY_PREFIX}steps:{TODAY.isoformat()}"}:
            assert rows[key] == later[table][key], (table, key)


def test_ninety_days_of_every_kind_and_nothing_after_now():
    sample = generate(TODAY, ZONE, NOW)
    counts = sample.counts()
    assert set(counts) == {model.__tablename__ for model in TABLES}
    assert counts["steps"] == DAYS
    assert counts["sleep_sessions"] == counts["resting_heart_rate"] == DAYS
    assert counts["heart_rate_variability"] == counts["oxygen_saturation"] == DAYS
    full_days = (DAYS - 1) * 24 * 60 // HEART_STEP
    today = (NOW - datetime(2026, 11, 10, 21, tzinfo=UTC)) // timedelta(minutes=HEART_STEP)
    assert counts["heart_rate"] == full_days + today
    # Five planned sessions a week, a few skipped; a weight every few days.
    assert 45 <= counts["exercise_sessions"] <= 65
    assert 25 <= counts["weight"] <= 60
    moments = [
        row.get("end_time") or row.get("timestamp")
        for model, rows in sample.rows.items()
        if model is not Steps
        for row in rows
    ]
    assert max(moments) <= NOW
    steps = {row["source_record_id"]: row["count"] for row in sample.rows[Steps]}
    assert 0 < steps[f"steps:{TODAY}"] < max(steps.values())


def test_every_row_says_it_is_sample_data_and_values_are_plausible():
    sample = generate(TODAY, ZONE, NOW)
    for rows in sample.rows.values():
        for row in rows:
            assert row["provider"] == DEMO
            assert row["google_data_point_id"].startswith(DEMO_KEY_PREFIX)
            assert row["device"] == DEVICE and "synthetic" in DEVICE
    assert all(4000 <= row["count"] <= 20000 for row in sample.rows[Steps][:-1])
    assert all(40 <= row["bpm"] <= 175 for row in sample.rows[HeartRate])
    nights = [row["end_time"] - row["start_time"] for row in sample.rows[SleepSession]]
    assert all(timedelta(hours=5) <= night <= timedelta(hours=9, minutes=30) for night in nights)
    assert all(Decimal(70) <= row["kilograms"] <= Decimal(76) for row in sample.rows[Weight])
    kinds = {row["exercise_type"] for row in sample.rows[ExerciseSession]}
    assert kinds == {"RUNNING", "STRENGTH_TRAINING", "CYCLING", "WALKING"}


async def test_loaded_sample_drives_the_dashboard_under_its_own_label(sessions):
    async with sessions.begin() as session:
        added = await load_sample(session, ZONE, NOW)
    assert added == generate(TODAY, ZONE, NOW).counts()
    async with sessions() as session:
        data = await dashboard_data(session, query=DashboardQuery(TODAY, 7, ZONE.key))
        workouts = await list_workouts(session, TODAY.replace(day=1), ZONE)
        status = await sample_status(session)
    days = data["days"]
    assert all(isinstance(day["steps"], int | float) for day in days)
    assert all(isinstance(day["resting_bpm"], int | float) for day in days)
    assert {day["sources"]["steps"]["source"] for day in days} == {DEMO}
    assert {day["sources"]["steps"]["label"] for day in days} == {"Örnek veri"}
    assert data["profile"]["weight"]["source"] == "Örnek veri"
    assert any(value is not None for value in data["charts"]["heart_rate"])
    assert workouts and {row["source"] for row in workouts} == {"Örnek veri"}
    assert status["loaded"] and status["last_day"] == TODAY
    assert status["first_day"] == TODAY - timedelta(days=DAYS - 1)


async def test_loading_again_adds_only_what_is_missing(sessions):
    async with sessions.begin() as session:
        await load_sample(session, ZONE, NOW)
    async with sessions.begin() as session:
        added = await load_sample(session, ZONE, NOW + timedelta(hours=2))
    assert added["steps"] == added["sleep_sessions"] == 0
    assert added["heart_rate"] == 2 * 60 // HEART_STEP


async def test_an_archive_with_device_data_is_refused(sessions):
    async with sessions.begin() as session:
        session.add(garmin_steps("garmin:steps:2026-11-10:v2"))
    with pytest.raises(SampleRefused):
        async with sessions.begin() as session:
            await load_sample(session, ZONE, NOW)
    async with sessions() as session:
        assert (await sample_status(session))["rows"] == 0


def garmin_steps(key, provider=GARMIN):
    return Steps(
        google_data_point_id=key,
        provider=provider,
        source_record_id="steps:2026-11-10",
        start_time=datetime(2026, 11, 10, tzinfo=UTC),
        end_time=datetime(2026, 11, 11, tzinfo=UTC),
        count=4321,
    )


async def counts(sessions):
    async with sessions() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (*TABLES, ManualMeasurement)
        }


async def test_removal_deletes_the_sample_and_keeps_every_other_record(sessions):
    async with sessions.begin() as session:
        await load_sample(session, ZONE, NOW)
        # Real records arriving after the sample: a Garmin day and a hand-entered weight.
        session.add(garmin_steps("garmin:steps:2026-11-10:v2"))
        session.add(ManualMeasurement(metric="weight", value=71, measured_at=NOW))
    async with sessions.begin() as session:
        removed = await remove_sample(session)
    assert removed == generate(TODAY, ZONE, NOW).counts()
    left = await counts(sessions)
    assert left["steps"] == 1 and left["manual_measurements"] == 1
    assert sum(left.values()) == 2
    async with sessions() as session:
        assert await session.scalar(select(Steps.provider)) == GARMIN
        assert (await sample_status(session))["loaded"] is False


@pytest.mark.parametrize(
    "key, provider",
    [("garmin:steps:2026-11-10:v2", DEMO), (f"{DEMO_KEY_PREFIX}steps:2020-01-01", GARMIN)],
)
async def test_a_row_marked_only_half_as_sample_refuses_the_removal(sessions, key, provider):
    async with sessions.begin() as session:
        await load_sample(session, ZONE, NOW)
    async with sessions.begin() as session:
        session.add(garmin_steps(key, provider))
    before = await counts(sessions)
    with pytest.raises(SampleRefused, match="hiçbir şey silinmedi"):
        async with sessions.begin() as session:
            await remove_sample(session)
    assert await counts(sessions) == before
