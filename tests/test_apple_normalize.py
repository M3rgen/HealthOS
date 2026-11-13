"""Apple Health normalization into the shared tables (app/services/apple_normalize.py).

Synthetic records (tests/apple_norm_records.py). Covers: the table and unit of each mapped
type, SDNN never stored as RMSSD, sleep stage rows (export and Health Auto Export,
summarized and not), workouts (export attributes or statistics, HAE v1/v2), the generic
``scalar_metrics`` fallback, skipped records, idempotent re-runs and re-normalizing the raw
archive (newest version per key, in pages).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apple_norm_records import (
    PHONE,
    PLUS_ONE,
    WATCH,
    activity_summary,
    category,
    correlation,
    hae_metric,
    hae_workout,
    quantity,
    sleep,
    stamp,
    workout,
)
from sqlalchemy import func, select

from app.core.providers import APPLE, apple_record_key
from app.models.health import (
    Distance,
    EnergyExpenditure,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RespiratoryRate,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
    Temperature,
    Vo2Max,
    Weight,
)
from app.services.providers import apple_archive
from app.services.providers.apple_normalize import normalize_archive, normalize_records, sleep_stage

T = datetime(2026, 11, 6, 7, 0, tzinfo=PLUS_ONE)


async def normalize(sessions, records):
    async with sessions.begin() as session:
        return await normalize_records(session, records)


async def all_rows(sessions, model):
    async with sessions() as session:
        return (await session.scalars(select(model).order_by(model.source_record_id))).all()


async def test_each_type_lands_in_its_table_in_its_unit(sessions):
    records = [
        quantity("StepCount", 120, "count", T, T + timedelta(minutes=5)),
        quantity("DistanceWalkingRunning", "1.5", "mi", T, T + timedelta(minutes=30)),
        quantity("ActiveEnergyBurned", "418.4", "kJ", T, T + timedelta(minutes=30)),
        quantity("BasalEnergyBurned", "60", "kcal", T, T + timedelta(hours=1)),
        quantity("HeartRate", 64, "count/min", T),
        quantity("RestingHeartRate", 52, "count/min", T),
        quantity("HeartRateVariabilitySDNN", "45.5", "ms", T),
        quantity("OxygenSaturation", "0.97", "%", T),
        quantity("RespiratoryRate", "14.5", "count/min", T),
        quantity("VO2Max", "44.1", "mL/min·kg", T),
        quantity("BodyMass", "176.37", "lb", T, source=PHONE),
        quantity("BodyTemperature", "98.6", "degF", T, source=PHONE),
        quantity("AppleSleepingWristTemperature", "35.2", "degC", T),
    ]
    stats = await normalize(sessions, records)
    assert stats.skipped == 0 and stats.inserted == 13
    (steps,) = await all_rows(sessions, Steps)
    assert steps.count == 120 and steps.provider == APPLE and steps.device == WATCH
    assert steps.source_record_id == f"export:HKQuantityTypeIdentifierStepCount:{records[0].key}"
    assert steps.google_data_point_id == apple_record_key("export", "quantity", records[0].key)
    assert steps.source_timezone == "+01:00"
    assert steps.start_time.replace(tzinfo=UTC) == datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    (distance,) = await all_rows(sessions, Distance)
    assert distance.meters == Decimal("2414.016")
    energy = {
        row.source_record_id.split(":")[1]: row.kcal
        for row in await all_rows(sessions, EnergyExpenditure)
    }
    # Active and basal stay apart by their type.
    assert energy["HKQuantityTypeIdentifierActiveEnergyBurned"] == Decimal("100.00000")
    assert energy["HKQuantityTypeIdentifierBasalEnergyBurned"] == Decimal("60")
    assert (await all_rows(sessions, HeartRate))[0].bpm == 64
    assert (await all_rows(sessions, RestingHeartRate))[0].bpm == 52
    (hrv,) = await all_rows(sessions, HeartRateVariability)
    assert hrv.rmssd is None and hrv.metrics == {"sdnn_ms": 45.5}
    assert (await all_rows(sessions, OxygenSaturation))[0].spo2 == Decimal("97.0000")
    (rate,) = await all_rows(sessions, RespiratoryRate)
    assert rate.breaths_per_minute == Decimal("14.5")
    assert rate.metric_type == "HKQuantityTypeIdentifierRespiratoryRate"
    assert (await all_rows(sessions, Vo2Max))[0].ml_per_kg_min == Decimal("44.1")
    (weight,) = await all_rows(sessions, Weight)
    assert weight.kilograms == Decimal("80.00009") and weight.device == PHONE
    temperatures = {row.metric_type: row.celsius for row in await all_rows(sessions, Temperature)}
    assert temperatures == {
        "HKQuantityTypeIdentifierBodyTemperature": Decimal("37.0000"),
        "HKQuantityTypeIdentifierAppleSleepingWristTemperature": Decimal("35.2000"),
    }


async def test_hae_names_map_like_export_identifiers(sessions):
    day = stamp(T)
    records = [
        hae_metric("step_count", "count", {"date": day, "qty": 900, "source": WATCH}),
        hae_metric("walking_running_distance", "km", {"date": day, "qty": 2.5, "source": WATCH}),
        hae_metric("active_energy", "kJ", {"date": day, "qty": 836.8, "source": WATCH}),
        hae_metric(
            "heart_rate",
            "count/min",
            {"date": day, "Min": 50, "Avg": 61, "Max": 90, "source": WATCH},
        ),
        hae_metric("heart_rate_variability", "ms", {"date": day, "qty": 38, "source": WATCH}),
        hae_metric("blood_oxygen_saturation", "%", {"date": day, "qty": 96, "source": WATCH}),
        hae_metric("weight_body_mass", "lb", {"date": day, "qty": 165, "source": PHONE}),
        hae_metric("body_temperature", "degF", {"date": day, "qty": 99.5, "source": PHONE}),
    ]
    stats = await normalize(sessions, records)
    assert stats.skipped == 0
    (steps,) = await all_rows(sessions, Steps)
    # A summarized point without an end and without an aggregation is an instant.
    assert steps.count == 900 and steps.start_time == steps.end_time
    assert steps.source_record_id.startswith("hae:step_count:")
    assert (await all_rows(sessions, Distance))[0].meters == Decimal("2500.0")
    assert (await all_rows(sessions, EnergyExpenditure))[0].kcal == Decimal("200.00000")
    assert (await all_rows(sessions, HeartRate))[0].bpm == 61  # Avg; Min/Max stay raw
    (hrv,) = await all_rows(sessions, HeartRateVariability)
    assert hrv.rmssd is None and hrv.metrics == {"sdnn_ms": 38.0}
    assert (await all_rows(sessions, OxygenSaturation))[0].spo2 == 96  # already a percent
    assert (await all_rows(sessions, Weight))[0].kilograms == Decimal("74.84274")
    assert (await all_rows(sessions, Temperature))[0].celsius == Decimal("37.5000")


async def test_a_summarized_point_spans_its_aggregation_bucket(sessions):
    point = {"date": stamp(T), "qty": 400, "source": WATCH}
    await normalize(sessions, [hae_metric("step_count", "count", point, aggregation="hours")])
    (steps,) = await all_rows(sessions, Steps)
    assert (steps.end_time - steps.start_time) == timedelta(hours=1)


async def test_normalizing_again_writes_nothing_and_a_newer_version_updates_in_place(sessions):
    records = [
        quantity("StepCount", 120, "count", T, T + timedelta(minutes=5)),
        sleep("AsleepCore", T - timedelta(hours=6), T - timedelta(hours=3)),
        workout("Running", T, T + timedelta(minutes=30), duration="30", durationUnit="min"),
        hae_metric("mystery_metric", "u", {"date": stamp(T), "qty": 3, "source": WATCH}),
    ]
    first = await normalize(sessions, records)
    assert first.inserted == 4 and first.updated == 0
    again = await normalize(sessions, records)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 4)
    # Health Auto Export re-sends a running total: same key, a new body; the last one wins.
    point = {"date": stamp(T), "qty": 100, "source": WATCH}
    old = hae_metric("step_count", "count", point)
    new = hae_metric("step_count", "count", {**point, "qty": 250})
    assert old.key == new.key
    assert (await normalize(sessions, [old, new])).inserted == 1
    async with sessions() as session:
        counts = (
            await session.scalars(select(Steps.count).where(Steps.source_record_id.like("hae:%")))
        ).all()
    assert counts == [250]
    updated = await normalize(sessions, [replace(new, body=old.body, digest="")])
    assert (updated.inserted, updated.updated) == (0, 1)


async def test_export_sleep_gives_one_row_per_segment_with_its_stage(sessions):
    night = datetime(2026, 11, 5, 23, 0, tzinfo=PLUS_ONE)
    records = [
        sleep("InBed", night - timedelta(minutes=30), night + timedelta(hours=8)),
        sleep("AsleepCore", night, night + timedelta(hours=2)),
        sleep("AsleepDeep", night + timedelta(hours=2), night + timedelta(hours=3)),
        sleep("Awake", night + timedelta(hours=3), night + timedelta(hours=3, minutes=10)),
        sleep("AsleepREM", night + timedelta(hours=3, minutes=10), night + timedelta(hours=4)),
        sleep("AsleepUnspecified", night + timedelta(hours=4), night + timedelta(hours=5)),
        sleep("Asleep", night + timedelta(hours=5), night + timedelta(hours=6)),
    ]
    await normalize(sessions, records)
    rows = {
        sleep_stage(row.source_record_id): row for row in await all_rows(sessions, SleepSession)
    }
    assert sorted(map(str, rows)) == ["asleep", "awake", "core", "deep", "in_bed", "rem"]
    assert rows["core"].light_sleep_seconds == 7200 and rows["core"].duration_seconds == 7200
    assert rows["deep"].deep_sleep_seconds == 3600
    assert rows["rem"].rem_sleep_seconds == 3000
    assert rows["awake"].awake_seconds == 600
    in_bed = rows["in_bed"]
    assert in_bed.duration_seconds == 30600 and in_bed.light_sleep_seconds is None
    assert in_bed.awake_seconds is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SleepSession)) == 7


async def test_hae_sleep_summarized_and_unsummarized(sessions):
    summarized = hae_metric(
        "sleep_analysis",
        "hr",
        {
            "date": "2026-11-06 00:00:00 +0100",
            "totalSleep": 7.25,
            "asleep": 0.25,
            "core": 4,
            "deep": 1.5,
            "rem": 1.5,
            "awake": 0.5,
            "sleepStart": "2026-11-05 23:10:00 +0100",
            "sleepEnd": "2026-11-06 07:00:00 +0100",
            "inBed": 8,
            "inBedStart": "2026-11-05 23:00:00 +0100",
            "inBedEnd": "2026-11-06 07:05:00 +0100",
            "source": WATCH,
        },
    )
    segment = hae_metric(
        "sleep_analysis",
        "hr",
        {
            "date": "2026-11-07 00:00:00 +0100",
            "start": "2026-11-06 23:00:00 +0100",
            "end": "2026-11-07 01:00:00 +0100",
            "startDate": "2026-11-06 23:00:00 +0100",
            "endDate": "2026-11-07 01:00:00 +0100",
            "qty": 2,
            "value": "Core",
            "source": WATCH,
        },
    )
    in_bed = hae_metric(
        "sleep_analysis",
        "hr",
        {
            "start": "2026-11-06 22:50:00 +0100",
            "end": "2026-11-07 06:00:00 +0100",
            "qty": 7.1,
            "value": "In Bed",
            "source": PHONE,
        },
    )
    assert (await normalize(sessions, [summarized, segment, in_bed])).skipped == 0
    rows = {
        sleep_stage(row.source_record_id): row for row in await all_rows(sessions, SleepSession)
    }
    night = rows["night"]
    assert night.duration_seconds == 26100 and night.light_sleep_seconds == 14400
    assert night.deep_sleep_seconds == 5400 and night.rem_sleep_seconds == 5400
    assert night.awake_seconds == 1800
    assert night.start_time.replace(tzinfo=UTC) == datetime(2026, 11, 5, 22, 10, tzinfo=UTC)
    assert rows["core"].light_sleep_seconds == 7200
    assert rows["in_bed"].device == PHONE


async def test_workouts_from_export_attributes_statistics_and_hae_versions(sessions):
    start = datetime(2026, 11, 6, 17, 0, tzinfo=PLUS_ONE)
    attributes = workout(
        "Running",
        start,
        start + timedelta(minutes=30),
        duration="30.5",
        durationUnit="min",
        totalDistance="3.1",
        totalDistanceUnit="mi",
        totalEnergyBurned="1046",
        totalEnergyBurnedUnit="kJ",
    )
    statistics = workout(
        "Cycling",
        start + timedelta(days=1),
        start + timedelta(days=1, hours=1),
        duration="3600",
        durationUnit="s",
        statistics=[
            {"type": "HKQuantityTypeIdentifierHeartRate", "average": "130", "unit": "count/min"},
            {"type": "HKQuantityTypeIdentifierActiveEnergyBurned", "sum": "410", "unit": "kcal"},
            {"type": "HKQuantityTypeIdentifierDistanceCycling", "sum": "20.5", "unit": "km"},
        ],
    )
    v2 = hae_workout(
        {
            "id": "W-1",
            "name": "Outdoor Walk",
            "start": "2026-11-08 09:00:00 +0100",
            "end": "2026-11-08 09:40:00 +0100",
            "duration": 2400,
            "source": {"name": WATCH, "identifier": "com.example"},
            "activeEnergyBurned": {"qty": 150, "units": "kcal"},
            "distance": {"qty": 1.2, "units": "mi"},
        }
    )
    v1 = hae_workout(
        {
            "name": "Yoga",
            "start": "2026-11-09 08:00:00 +0100",
            "end": "2026-11-09 08:45:00 +0100",
            "activeEnergy": {"qty": 418.4, "units": "kJ"},
        },
        version="1",
    )
    assert (await normalize(sessions, [attributes, statistics, v2, v1])).skipped == 0
    rows = {row.exercise_type: row for row in await all_rows(sessions, ExerciseSession)}
    running = rows["Running"]
    assert running.duration_seconds == 1830 and running.distance_meters == Decimal("4988.9664")
    assert running.calories_kcal == Decimal("250.0000")
    cycling = rows["Cycling"]
    assert cycling.calories_kcal == 410 and cycling.distance_meters == 20500
    walk = rows["Outdoor Walk"]
    assert walk.duration_seconds == 2400 and walk.calories_kcal == 150
    assert walk.distance_meters == Decimal("1931.2128") and walk.device == WATCH
    assert walk.source_record_id.startswith("hae:Outdoor Walk:")
    yoga = rows["Yoga"]
    assert yoga.duration_seconds == 2700 and yoga.calories_kcal == Decimal("100.0000")


async def test_everything_else_goes_to_scalar_metrics_and_unusable_records_are_counted(
    sessions,
):
    records = [
        quantity("FlightsClimbed", 3, "count", T, T + timedelta(minutes=2)),
        hae_metric("mystery_metric", "widgets", {"date": stamp(T), "qty": 7, "source": WATCH}),
        hae_metric(
            "blood_pressure",
            "mmHg",
            {"date": stamp(T), "systolic": 118, "diastolic": 76, "source": PHONE},
        ),
        category("AppleStandHour", "HKCategoryValueAppleStandHourStood", T, T + timedelta(hours=1)),
        activity_summary(
            "2026-11-06",
            activeEnergyBurned="410.5",
            activeEnergyBurnedGoal="500",
            activeEnergyBurnedUnit="kcal",
            appleExerciseTime="32",
            appleStandHours="10",
        ),
        # Skipped: a correlation (its children are records of their own), no time, no value.
        correlation(T),
        hae_metric("step_count", "count", {"qty": 5, "source": WATCH}),
        hae_metric(
            "mystery_metric",
            "widgets",
            {"date": stamp(T + timedelta(hours=1)), "note": "x", "source": WATCH},
        ),
    ]
    stats = await normalize(sessions, records)
    assert stats.skipped == 3
    rows = {(row.data_type, row.unit): row for row in await all_rows(sessions, ScalarMetric)}
    assert rows[("apple:HKQuantityTypeIdentifierFlightsClimbed", "count")].value == 3
    assert rows[("apple:mystery_metric", "widgets")].value == 7
    assert rows[("apple:blood_pressure:systolic", "mmHg")].value == 118
    assert rows[("apple:blood_pressure:diastolic", "mmHg")].value == 76
    stand = rows[("apple:HKCategoryTypeIdentifierAppleStandHour", None)]
    assert stand.value is None
    assert stand.attributes == {"value": "HKCategoryValueAppleStandHourStood"}
    energy = rows[("apple:ActivitySummary:activeEnergyBurned", "kcal")]
    assert energy.value == Decimal("410.5")
    assert energy.start_time.replace(tzinfo=UTC) == datetime(2026, 11, 6, tzinfo=UTC)
    assert rows[("apple:ActivitySummary:appleExerciseTime", "min")].value == 32
    assert rows[("apple:ActivitySummary:appleStandHours", "count")].value == 10
    assert ("apple:ActivitySummary:activeEnergyBurnedGoal", "kcal") in rows
    assert len(rows) == 9


async def test_the_archive_is_normalized_again_newest_version_first_in_pages(sessions):
    point = {"date": stamp(T), "qty": 100, "source": WATCH}
    old = hae_metric("step_count", "count", point)
    new = hae_metric("step_count", "count", {**point, "qty": 300})
    others = [
        quantity("StepCount", 50 + index, "count", T + timedelta(minutes=index))
        for index in range(4)
    ]
    async with sessions.begin() as session:
        await apple_archive.write_records(session, [old, *others])
    async with sessions.begin() as session:
        await apple_archive.write_records(session, [new])
    async with sessions.begin() as session:
        stats = await normalize_archive(session, batch=2)
    assert stats.inserted == 5 and stats.skipped == 0
    async with sessions() as session:
        hae = (
            await session.scalars(select(Steps.count).where(Steps.source_record_id.like("hae:%")))
        ).all()
    assert hae == [300]
    async with sessions.begin() as session:
        again = await normalize_archive(session, batch=2)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 5)
    async with sessions.begin() as session:
        only_hae = await normalize_archive(session, channel="hae")
        window = await normalize_archive(
            session,
            since=datetime(2026, 11, 6, 6, 2, tzinfo=UTC),
            until=datetime(2026, 11, 6, 7, 0, tzinfo=UTC),
        )
    assert only_hae.unchanged == 1 and window.unchanged == 2


async def test_only_apple_rows_are_ever_written_onto(sessions):
    record = quantity("StepCount", 10, "count", T, T + timedelta(minutes=1))
    async with sessions.begin() as session:
        session.add(
            Steps(
                google_data_point_id=apple_record_key("export", "quantity", record.key),
                start_time=T,
                end_time=T,
                count=Decimal(99),
            )
        )
    stats = await normalize(sessions, [record])
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 0, 1)
    (row,) = await all_rows(sessions, Steps)
    assert row.count == 99 and row.provider == "GOOGLE"
