"""Garmin normalization into the shared tables and the provider-aware readers around it.

Synthetic payloads only (tests/garmin_records.py shapes). Covers: the rows a window yields,
idempotent re-runs, the Google readers' blindness to Garmin rows (dashboard values, coverage,
deletion marking), the resolver on the dashboard, workouts and devices, the Garmin daily
projection's precedence line and the identity layer keeping GARMIN apart from FITBIT.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from garmin_records import activity, daily_steps, daily_stress, daily_summary, heart_rate, sleep
from identity_records import STEPS, fitbit, steps, store
from sqlalchemy import func, select

from app.api.dashboard import place_workouts, workouts
from app.core.config import Settings
from app.core.providers import GARMIN, GOOGLE, garmin_record_key
from app.models.garmin import GarminPullState, GarminRawRecord
from app.models.health import (
    Distance,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
)
from app.models.state import RawHealthData
from app.parsers.identity import kindred, stable_class, stable_source
from app.services import garmin_normalize, garmin_readers
from app.services.coverage import coverage_response
from app.services.dashboard import dashboard_data
from app.services.devices import device_report
from app.services.garmin_normalize import normalize_window
from app.services.garmin_pull import KIND_NAMES, GarminPull
from app.services.source_precedence import PRIMARY, SECONDARY, Policy
from app.sync.deletions import mark_source_deletions

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
# Garmin heart-rate samples and the sleep of DAY, in milliseconds since the epoch (UTC).
MS = int(datetime(2026, 11, 6, 0, 0, tzinfo=UTC).timestamp() * 1000)
REVERSED = Policy(activity=("FITBIT", "GARMIN"), health=("GARMIN", "FITBIT"))


def raw(kind: str, key: str, body, seen: datetime = NOW, suffix: str = "a") -> GarminRawRecord:
    return GarminRawRecord(
        kind=kind,
        key=key,
        body=body,
        body_sha256=(suffix * 64)[:64] if len(suffix) == 1 else suffix,
        first_seen_at=seen,
    )


def garmin_heart(day: date, samples: list[tuple[int, int | None]]) -> dict:
    record = heart_rate(day)
    record["startTimestampGMT"] = f"{day.isoformat()}T00:00:00.0"
    record["endTimestampGMT"] = f"{day.isoformat()}T23:59:59.0"
    record["heartRateValues"] = [[MS + minute * 60_000, bpm] for minute, bpm in samples]
    return record


def garmin_sleep(day: date) -> dict:
    record = sleep(day)
    # 22:00 the night before to 06:00: ends on DAY.
    record["sleepStartTimestampGMT"] = MS - 2 * 3_600_000
    record["sleepEndTimestampGMT"] = MS + 6 * 3_600_000
    return record


def garmin_activity(activity_id: int, day: date, start: str = "17:05:00", duration=2410.0):
    record = activity(activity_id, day)
    record["startTimeGMT"] = f"{day.isoformat()} {start}"
    record["duration"] = duration
    return record


async def archive_day(sessions, day: date = DAY) -> None:
    """One synthetic Garmin day in the raw archive: every kind the normalization reads."""
    async with sessions.begin() as session:
        session.add_all(
            [
                raw("steps", day.isoformat(), daily_steps(day, steps=7400), suffix="1"),
                raw("daily-summaries", day.isoformat(), daily_summary(day, resting=52), suffix="2"),
                raw(
                    "heart-rate",
                    day.isoformat(),
                    garmin_heart(day, [(0, 61), (1, 64), (2, None)]),
                    suffix="3",
                ),
                raw("sleep", day.isoformat(), garmin_sleep(day), suffix="4"),
                raw(
                    "hrv-summaries",
                    day.isoformat(),
                    {
                        "calendarDate": day.isoformat(),
                        "lastNightAvg": 43,
                        "status": "UNBALANCED",
                        "lastNight5MinHigh": 61,
                    },
                    suffix="5",
                ),
                raw(
                    "spo2",
                    day.isoformat(),
                    {"calendarDate": day.isoformat(), "averageSpO2": 96.5},
                    suffix="6",
                ),
                raw("stress", day.isoformat(), daily_stress(day), suffix="7"),
                raw("activities", "1001", garmin_activity(1001, day), suffix="8"),
                GarminPullState(kind="steps", last_success_at=NOW, last_attempt_at=NOW),
            ]
        )


async def count(sessions, model, **where) -> int:
    async with sessions() as session:
        query = select(func.count()).select_from(model)
        for name, value in where.items():
            query = query.where(getattr(model, name) == value)
        return await session.scalar(query) or 0


async def test_a_day_becomes_tagged_rows_in_the_shared_tables_once(sessions):
    await archive_day(sessions)
    async with sessions.begin() as session:
        stats = await normalize_window(session, DAY, DAY, device="Garmin Test")
    assert stats.skipped == 0 and stats.updated == 0
    assert stats.tables == {
        "distance": {"inserted": 1, "updated": 0, "unchanged": 0},
        "exercise_sessions": {"inserted": 1, "updated": 0, "unchanged": 0},
        "heart_rate": {"inserted": 2, "updated": 0, "unchanged": 0},  # the null sample skipped
        "heart_rate_variability": {"inserted": 1, "updated": 0, "unchanged": 0},
        "oxygen_saturation": {"inserted": 1, "updated": 0, "unchanged": 0},
        "resting_heart_rate": {"inserted": 1, "updated": 0, "unchanged": 0},
        "scalar_metrics": {"inserted": 2, "updated": 0, "unchanged": 0},
        "sleep_sessions": {"inserted": 1, "updated": 0, "unchanged": 0},
        "steps": {"inserted": 1, "updated": 0, "unchanged": 0},
    }
    async with sessions() as session:
        steps = (await session.scalars(select(Steps))).one()
        assert steps.provider == GARMIN and steps.device == "Garmin Test"
        assert steps.google_data_point_id == garmin_record_key("steps", DAY.isoformat())
        assert steps.source_record_id == f"steps:{DAY.isoformat()}"
        assert steps.count == 7400
        # The civil day's UTC bounds from the heart-rate record, half open.
        assert (steps.start_time.replace(tzinfo=UTC), steps.end_time.replace(tzinfo=UTC)) == (
            datetime(2026, 11, 6, tzinfo=UTC),
            datetime(2026, 11, 7, tzinfo=UTC),
        )
        distance = (await session.scalars(select(Distance))).one()
        assert distance.meters == Decimal("5476")  # 7400 * 0.74
        samples = (await session.scalars(select(HeartRate).order_by(HeartRate.timestamp))).all()
        assert [int(s.bpm) for s in samples] == [61, 64]
        assert samples[0].google_data_point_id == garmin_record_key(
            "heart-rate", DAY.isoformat(), str(MS)
        )
        resting = (await session.scalars(select(RestingHeartRate))).one()
        assert resting.bpm == 52 and resting.timestamp.replace(tzinfo=UTC) == datetime(
            2026, 11, 6, tzinfo=UTC
        )
        night = (await session.scalars(select(SleepSession))).one()
        assert (night.duration_seconds, night.deep_sleep_seconds, night.awake_seconds) == (
            25200,
            5400,
            600,
        )
        hrv = (await session.scalars(select(HeartRateVariability))).one()
        assert hrv.rmssd == 43 and hrv.metrics == {"status": "UNBALANCED", "lastNight5MinHigh": 61}
        scalars = {
            row.data_type: row for row in (await session.scalars(select(ScalarMetric))).all()
        }
        assert (
            scalars["garmin-stress"].value == 24
            and scalars["garmin-stress"].attributes["restStressDuration"] == 31000
        )
        assert (
            scalars["garmin-body-battery"].value == 42
            and scalars["garmin-body-battery"].attributes["max"] == 88
        )
        workout = (await session.scalars(select(ExerciseSession))).one()
        assert workout.exercise_type == "walking" and workout.provider == GARMIN
        assert workout.end_time.replace(tzinfo=UTC) - workout.start_time.replace(
            tzinfo=UTC
        ) == timedelta(seconds=2410)
        assert workout.calories_kcal == 210 and workout.distance_meters == Decimal("3120.5")
        # Nothing Google-shaped was written.
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0


async def test_normalizing_again_writes_nothing_and_a_new_payload_version_updates_in_place(
    sessions,
):
    await archive_day(sessions)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions.begin() as session:
        again = await normalize_window(session, DAY, DAY)
    assert again.inserted == 0 and again.updated == 0 and again.unchanged == 11
    async with sessions() as session:
        stamps = (await session.execute(select(Steps.updated_at, Steps.id))).all()
    # Garmin corrected the day: a new raw version (the earlier row stays), one updated row.
    async with sessions.begin() as session:
        session.add(
            raw(
                "steps",
                DAY.isoformat(),
                daily_steps(DAY, steps=7512),
                seen=NOW + timedelta(hours=2),
                suffix="9",
            )
        )
    async with sessions.begin() as session:
        third = await normalize_window(session, DAY, DAY)
    assert third.inserted == 0 and third.updated == 2  # steps and distance changed
    async with sessions() as session:
        steps = (await session.scalars(select(Steps))).one()
        assert steps.count == 7512 and steps.id == stamps[0][1]  # the same row, in place
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GarminRawRecord)
                .where(GarminRawRecord.kind == "steps")
            )
            == 2
        )


async def test_a_record_without_what_a_row_needs_is_skipped_not_invented(sessions):
    async with sessions.begin() as session:
        session.add_all(
            [
                raw(
                    "sleep",
                    DAY.isoformat(),
                    {"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 100},
                    suffix="1",
                ),
                raw("activities", "7", {"activityId": 7, "activityName": "no start"}, suffix="2"),
                raw(
                    "spo2",
                    DAY.isoformat(),
                    {"calendarDate": DAY.isoformat(), "averageSpO2": None},
                    suffix="3",
                ),
            ]
        )
    async with sessions.begin() as session:
        stats = await normalize_window(session, DAY, DAY)
    assert stats.inserted == 0 and stats.skipped == 1  # the activity; the others yield no candidate
    assert (
        garmin_normalize.activity_row(
            {"activityId": 7, "startTimeGMT": "2026-11-06 10:00:00", "duration": 0}, "Garmin"
        )
        is None
    )
    assert garmin_normalize.day_bounds(DAY, None) == (
        datetime(2026, 11, 6, tzinfo=UTC),
        datetime(2026, 11, 7, tzinfo=UTC),
    )


async def test_pull_service_normalizes_with_the_configured_device_name(sessions, tmp_path):
    """The service's ``normalize`` (run after every pull and backfill) tags rows with the
    configured device name and writes nothing the second time."""
    await archive_day(sessions)
    key = tmp_path / "key"
    key.write_text("synthetic")
    settings = Settings(
        _env_file=None,
        garmin_enabled=True,
        garmin_api_key_file=key,
        garmin_device_name="Forerunner Test",
    )
    pull = GarminPull(settings, sessions, clock=lambda: NOW)
    stats = await pull.normalize(DAY, DAY)
    assert stats.inserted == 11
    async with sessions() as session:
        assert (await session.scalars(select(Steps.device))).one() == "Forerunner Test"
    again = await pull.normalize(DAY, DAY)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 11)


async def test_google_readers_never_see_garmin_rows(sessions, clock):
    """Dashboard, coverage and deletion marking read Google through raw_health_data; a Garmin
    row has none, so none of them counts, lists or marks it."""
    fetch = datetime(2026, 11, 6, 8, 0, tzinfo=UTC)
    clock.now = fetch
    google = steps(8000, datetime(2026, 11, 6, 7, 0, tzinfo=UTC), source=fitbit("Fitbit Air"))
    await store(sessions, STEPS, google, fetch_started=fetch)
    async with sessions() as session:
        before_dashboard = await dashboard_data(session, DAY, 1, "UTC")
        before_coverage = await coverage_response(
            session, DAY - timedelta(days=1), DAY, "UTC", "steps"
        )
    await archive_day(sessions)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        after = await dashboard_data(session, DAY, 1, "UTC")
        coverage = await coverage_response(session, DAY - timedelta(days=1), DAY, "UTC", "steps")
    # The Google value is selected (health: Fitbit first) and is the Google value alone.
    assert after["days"][0]["steps"] == 8000 == before_dashboard["days"][0]["steps"]
    assert after["days"][0]["sources"]["steps"]["alternatives"] == [
        {"source": GARMIN, "label": "Garmin", "value": 7400}
    ]
    assert "GARMIN" not in json.dumps(coverage, default=str) and "Garmin" not in json.dumps(
        coverage, default=str
    )

    def strip(card: dict) -> str:
        # The card's own timestamps (checked at) differ between the two reads; nothing else may.
        return re.sub(
            r'"\d{4}-\d{2}-\d{2}T[^"]+"', '"<t>"', json.dumps(card, default=str, sort_keys=True)
        )

    assert strip(coverage) == strip(before_coverage)
    # A complete Google fetch that returns nothing marks the Google row and touches no Garmin row.
    clock.now = fetch + timedelta(hours=1)
    async with sessions.begin() as session:
        marked = await mark_source_deletions(
            session,
            STEPS,
            datetime(2026, 11, 6, tzinfo=UTC),
            datetime(2026, 11, 7, tzinfo=UTC),
            fetch + timedelta(minutes=30),
        )
    assert marked == 1
    assert (
        await count(sessions, Steps, provider=GARMIN) == 1
        and await count(sessions, GarminRawRecord) == 8
    )
    async with sessions() as session:
        garmin_steps = (await session.scalars(select(Steps).where(Steps.provider == GARMIN))).one()
        assert garmin_steps.count == 7400 and garmin_steps.is_quarantined is False
        final = await dashboard_data(session, DAY, 1, "UTC")
    # With the Google row gone, Garmin's value is shown, alone, not added to anything.
    assert (
        final["days"][0]["steps"] == 7400
        and final["days"][0]["sources"]["steps"]["source"] == GARMIN
    )
    assert final["days"][0]["sources"]["steps"]["alternatives"] == []


async def test_dashboard_resolves_every_metric_and_the_policy_can_be_reversed(sessions):
    google_rhr = RawHealthData(
        data_type="daily-resting-heart-rate",
        google_data_point_id="rhr",
        raw_json={
            "dataSource": fitbit("Fitbit Air"),
            "dailyRestingHeartRate": {"date": DAY.isoformat(), "beatsPerMinute": 58},
        },
        source_start_time=datetime(2026, 11, 6, tzinfo=UTC),
        payload_hash="x",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    async with sessions.begin() as session:
        session.add(google_rhr)
    await archive_day(sessions)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        default = await dashboard_data(session, DAY, 1, "UTC")
        reversed_ = await dashboard_data(session, DAY, 1, "UTC", policy=REVERSED)
    day = default["days"][0]
    assert day["resting_bpm"] == 58 and day["sources"]["resting_bpm"]["source"] == "FITBIT"
    assert day["sources"]["resting_bpm"]["alternatives"] == [
        {"source": GARMIN, "label": "Garmin", "value": 52.0}
    ]
    # Metrics only Garmin measured are shown from Garmin (no Google value to prefer).
    assert day["steps"] == 7400 and day["sources"]["steps"]["source"] == GARMIN
    assert day["sleep_seconds"] == 25200 - 600 and day["sleep_assumptions"] == [
        "from_normalized_columns"
    ]
    assert day["hrv_ms"] == 43 and day["spo2"] == 96.5
    assert day["status"] == "complete"
    assert default["precedence"] == Policy().as_dict()
    flipped = reversed_["days"][0]
    assert flipped["resting_bpm"] == 52 and flipped["sources"]["resting_bpm"]["source"] == GARMIN
    assert flipped["sources"]["resting_bpm"]["alternatives"][0]["value"] == 58.0
    # The hourly heart chart: Garmin is the only source with samples here.
    assert (
        default["charts"]["heart_rate"][0] == 62.5
        and default["charts"]["sources"]["heart_rate"]["source"] == GARMIN
    )
    assert default["charts"]["sources"]["heart_rate"]["activity_windows"] == GARMIN


async def test_heart_chart_takes_fitbit_outside_and_garmin_inside_a_garmin_workout(sessions, clock):
    from identity_records import HEART, heart

    minute = datetime(2026, 11, 6, 17, 10, tzinfo=UTC)  # inside the 17:05 walk
    outside = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    await store(
        sessions,
        HEART,
        heart(70, outside, name="Fitbit Air"),
        heart(100, minute, name="Fitbit Air"),
    )
    await archive_day(sessions)
    async with sessions.begin() as session:
        session.add(
            raw(
                "heart-rate",
                DAY.isoformat(),
                garmin_heart(DAY, [(9 * 60, 80), (17 * 60 + 10, 120)]),
                seen=NOW + timedelta(hours=1),
                suffix="h",
            )
        )
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        data = await dashboard_data(session, DAY, 1, "UTC")
    chart = data["charts"]["heart_rate"]
    assert chart[9] == 70.0  # health policy: the Fitbit sample, Garmin's 80 not averaged in
    assert chart[17] == 120.0  # inside the Garmin workout: the Garmin sample
    sources = data["charts"]["sources"]["heart_rate"]
    assert sources["source"] == "FITBIT" and sources["alternatives"] == [
        {"source": GARMIN, "label": "Garmin"}
    ]
    assert sources["minutes_by_source"] == {"FITBIT": 1, GARMIN: 1}


async def test_workouts_list_garmin_first_with_the_overlapping_fitbit_session_as_alternative(
    sessions, clock
):
    from app.google_health.data_types import REGISTRY

    exercise = REGISTRY["exercise"]
    fitbit_walk = {
        "dataSource": fitbit("Fitbit Air"),
        "exercise": {
            "interval": {"startTime": "2026-11-06T17:10:00Z", "endTime": "2026-11-06T17:30:00Z"},
            "exerciseType": "WALKING",
        },
    }
    await store(sessions, exercise, fitbit_walk)
    await archive_day(sessions)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        rows = await workouts(month=DAY.replace(day=1), timezone="UTC", session=session)
    by_source = {row["source_code"]: row for row in rows}
    assert set(by_source) == {GARMIN, "FITBIT"} and len(rows) == 2
    garmin, fitbit_row = by_source[GARMIN], by_source["FITBIT"]
    assert (
        garmin["source"] == "Garmin"
        and garmin["priority"] == PRIMARY
        and garmin["provider"] == GARMIN
    )
    assert fitbit_row["source"] == "Fitbit" and fitbit_row["priority"] == SECONDARY
    assert fitbit_row["group"] == garmin["id"] and fitbit_row["alternatives"] == []
    assert [item["id"] for item in garmin["alternatives"]] == [fitbit_row["id"]]
    assert garmin["alternatives"][0]["source"] == "Fitbit"
    # Reversed policy: the Fitbit session leads.
    placed = place_workouts([dict(row) for row in rows], REVERSED)
    assert {row["source_code"]: row["priority"] for row in placed} == {
        GARMIN: SECONDARY,
        "FITBIT": PRIMARY,
    }


async def test_devices_list_the_garmin_source_with_its_precedence(sessions, clock):
    await store(
        sessions,
        STEPS,
        steps(10, datetime(2026, 11, 6, 7, 0, tzinfo=UTC), source=fitbit("Fitbit Air")),
    )
    async with sessions() as session:
        assert (await device_report(session, now=NOW))["garmin"] == "off"
    await archive_day(sessions)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        report = await device_report(session, now=NOW, garmin_name="Forerunner Test")
    assert report["garmin"] == "ok" and report["precedence"] == Policy().as_dict()
    by_name = {device["name"]: device for device in report["devices"]}
    garmin = by_name["Forerunner Test"]
    assert (
        garmin["id"] == "garmin"
        and garmin["provider"] == GARMIN
        and garmin["source_code"] == GARMIN
    )
    assert garmin["precedence"] == {"activity": PRIMARY, "health": SECONDARY}
    assert garmin["data_types"] == [
        "activities",
        "daily-summaries",
        "heart-rate",
        "hrv-summaries",
        "sleep",
        "spo2",
        "steps",
        "stress",
    ]
    assert garmin["garmin_last_pull"] == NOW.isoformat() and garmin["status"] == "bagli"
    assert (
        garmin["last_data"] == datetime(2026, 11, 7, tzinfo=UTC).isoformat()
    )  # the steps day's end
    watch = by_name["Fitbit Air"]
    assert watch["provider"] == GOOGLE and watch["source_code"] == "FITBIT"
    assert watch["precedence"] == {"activity": SECONDARY, "health": PRIMARY}


async def test_garmin_daily_projection_says_where_garmin_stands(sessions, tmp_path):
    await archive_day(sessions)
    key = tmp_path / "key"
    key.write_text("synthetic")
    default = GarminPull(
        Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key), sessions
    )
    card = await default.daily(DAY, DAY)
    assert (
        card["precedence"]["steps"] == SECONDARY
        and card["precedence"]["sleep_seconds"] == SECONDARY
    )
    assert card["policy"] == Policy().as_dict() and card["days"][0]["steps"] == 7400
    flipped = GarminPull(
        Settings(
            _env_file=None,
            garmin_enabled=True,
            garmin_api_key_file=key,
            source_precedence_health="GARMIN,FITBIT",
        ),
        sessions,
    )
    assert (await flipped.daily(DAY, DAY))["precedence"]["steps"] == PRIMARY


def test_identity_keeps_garmin_apart_from_fitbit():
    """Were a Garmin record ever to carry a Google-shaped dataSource, its platform keeps it
    from chaining with the Fitbit: the stable class differs and the two are not kindred."""
    garmin = {
        "platform": "GARMIN",
        "recordingMethod": "AUTOMATICALLY_RECORDED",
        "device": {"displayName": "Forerunner"},
    }
    assert stable_source(garmin)["platform"] == "GARMIN" != stable_source(fitbit())["platform"]
    assert stable_class(garmin) != stable_class(fitbit())
    assert not kindred(garmin, fitbit()) and not kindred(garmin, fitbit(None))


def test_umay_tool_description_states_the_rule():
    from app.services.codex_health import HEALTH_TOOL

    text = HEALTH_TOOL["description"]
    assert "Garmin first" in text and "Fitbit first" in text and "never summed" in text


async def test_readers_report_only_garmin_rows(sessions):
    """The Garmin readers select by provider: a Google row with a day-shaped key is not theirs."""
    async with sessions.begin() as session:
        session.add(
            Steps(
                google_data_point_id="synthetic:g",
                start_time=NOW,
                end_time=NOW,
                count=Decimal(5),
                source_record_id=f"steps:{DAY.isoformat()}",
            )
        )
    async with sessions() as session:
        from zoneinfo import ZoneInfo

        assert await garmin_readers.daily_values(session, DAY, DAY, ZoneInfo("UTC")) == {}
        assert await garmin_readers.archive_summary(session) is None
        assert await garmin_readers.workouts(session, NOW - timedelta(days=30), NOW) == []


async def test_backfill_normalizes_activities_archived_before_normalization_existed(
    sessions, tmp_path
):
    """A phase-1 archive (activities first seen long before this run) gets its activities into
    ``exercise_sessions`` from a backfill over their window; the second run writes nothing."""
    key = tmp_path / "key"
    key.write_text("synthetic")
    old = NOW - timedelta(days=120)
    async with sessions.begin() as session:
        session.add_all(
            [
                raw("activities", "4001", garmin_activity(4001, DAY), seen=old, suffix="a"),
                raw(
                    "activities",
                    "4002",
                    garmin_activity(4002, DAY - timedelta(days=30)),
                    seen=old,
                    suffix="b",
                ),
                raw("steps", DAY.isoformat(), daily_steps(DAY), seen=old, suffix="c"),
                # Every kind already finished these bounds: the backfill fetches nothing.
                *(
                    GarminPullState(
                        kind=name,
                        backfill_since=DAY - timedelta(days=60),
                        backfill_until=DAY,
                        backfill_next=None,
                    )
                    for name in KIND_NAMES
                ),
            ]
        )
    pull = GarminPull(
        Settings(_env_file=None, garmin_enabled=True, garmin_api_key_file=key),
        sessions,
        clock=lambda: NOW,
    )
    report = await pull.backfill(DAY - timedelta(days=60), DAY)
    assert all(entry["requests"] == 0 and entry["complete"] for entry in report["kinds"].values())
    assert report["normalized"]["tables"]["exercise_sessions"]["inserted"] == 2
    assert report["normalized"]["tables"]["steps"]["inserted"] == 1
    assert await count(sessions, ExerciseSession, provider=GARMIN) == 2
    again = await pull.backfill(DAY - timedelta(days=60), DAY)
    assert again["normalized"]["inserted"] == 0 and again["normalized"]["updated"] == 0
    # The periodic pull still reads only the activities it archived itself.
    stats = await pull.normalize(DAY, DAY, seen_since=NOW)
    assert stats.tables.get("exercise_sessions") is None and stats.unchanged >= 1
