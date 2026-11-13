"""The Apple readers (app/services/apple_readers.py): no double counting.

Synthetic records normalized through app/services/apple_normalize.py, then read back: a
Watch and an iPhone counting the same steps, both channels carrying the same day, In Bed
overlapping the asleep stages, the wake-up day across a DST change, daily means, heart-rate
samples, workouts both channels carry, and the sources list.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from apple_norm_records import PHONE, PLUS_ONE, WATCH, hae_metric, quantity, sleep, stamp, workout
from apple_norm_records import hae_workout as hae_workout_record

from app.models.apple import AppleSource
from app.services.providers import apple_readers
from app.services.providers.apple_normalize import normalize_records

DAY = date(2026, 11, 6)
ZONE = ZoneInfo("Europe/Brussels")


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PLUS_ONE)


async def normalize(sessions, *records):
    async with sessions.begin() as session:
        await normalize_records(session, list(records))


async def daily(sessions, first=DAY, last=DAY, zone=ZONE):
    async with sessions() as session:
        return await apple_readers.daily_values(session, first, last, zone)


async def test_watch_and_iphone_steps_are_not_added(sessions):
    await normalize(
        sessions,
        # 07:00-08:00: both count the same walk; the Watch counted more.
        quantity("StepCount", 1000, "count", at(7), at(8)),
        quantity("StepCount", 800, "count", at(7), at(8), source=PHONE),
        # 12:00-13:00: only the iPhone (the Watch was off).
        quantity("StepCount", 300, "count", at(12), at(12, 30), source=PHONE),
        # 23:30-00:30: half of it belongs to the next day.
        quantity("StepCount", 200, "count", at(23, 30), at(23, 30) + timedelta(hours=1)),
    )
    days = await daily(sessions, DAY, DAY + timedelta(days=1))
    assert days[DAY]["steps"] == 1400
    assert days[DAY + timedelta(days=1)]["steps"] == 100


async def test_both_channels_of_one_health_store_are_not_added(sessions):
    await normalize(
        sessions,
        quantity("StepCount", 1000, "count", at(7), at(8)),
        quantity("StepCount", 400, "count", at(9), at(10)),
        # The same day from Health Auto Export, summarized by day (HealthKit deduplicated it).
        hae_metric(
            "step_count",
            "count",
            {"date": stamp(at(0)), "qty": 1450, "source": f"{WATCH}|{PHONE}"},
            aggregation="days",
        ),
    )
    days = await daily(sessions)
    assert days[DAY]["steps"] == 1450  # the larger channel, not 2850


async def test_sleep_takes_the_asleep_union_of_the_best_source_never_in_bed(sessions):
    night = at(23, day=DAY - timedelta(days=1))
    await normalize(
        sessions,
        sleep("InBed", night - timedelta(minutes=30), night + timedelta(hours=8)),
        sleep("AsleepCore", night, night + timedelta(hours=3)),
        sleep("AsleepDeep", night + timedelta(hours=3), night + timedelta(hours=4)),
        sleep("Awake", night + timedelta(hours=4), night + timedelta(hours=4, minutes=20)),
        sleep("AsleepREM", night + timedelta(hours=4, minutes=20), night + timedelta(hours=6)),
        # A duplicate Core segment (overlapping the first): counted once.
        sleep("AsleepCore", night + timedelta(hours=1), night + timedelta(hours=2)),
        # A phone app saw less of the same night: never added to the Watch's.
        sleep("AsleepUnspecified", night, night + timedelta(hours=5), source=PHONE),
        sleep("InBed", night, night + timedelta(hours=9), source=PHONE),
    )
    days = await daily(sessions)
    assert days[DAY]["sleep_seconds"] == (6 * 60 - 20) * 60
    assert DAY - timedelta(days=1) not in days


async def test_hae_summarized_and_export_nights_are_not_added(sessions):
    night = at(23, day=DAY - timedelta(days=1))
    await normalize(
        sessions,
        sleep("AsleepCore", night, night + timedelta(hours=7)),
        hae_metric(
            "sleep_analysis",
            "hr",
            {
                "date": stamp(at(0)),
                "totalSleep": 7.5,
                "sleepStart": stamp(night),
                "sleepEnd": stamp(night + timedelta(hours=8)),
                "source": WATCH,
            },
        ),
        # The same night unsummarized too (the setting was changed): not added to the summary.
        hae_metric(
            "sleep_analysis",
            "hr",
            {
                "start": stamp(night),
                "end": stamp(night + timedelta(hours=7)),
                "qty": 7,
                "value": "Asleep",
                "source": WATCH,
            },
        ),
    )
    assert (await daily(sessions))[DAY]["sleep_seconds"] == 7.5 * 3600


async def test_wake_day_across_the_dst_change(sessions):
    """Brussels leaves summer time on 25 October 2026 at 03:00 (+02:00 -> +01:00): a night
    from 23:00 on the 24th to 07:00 on the 25th is nine hours and belongs to the 25th, the
    part before midnight included; the 25th has 25 local hours of steps."""
    change = date(2026, 10, 25)
    before = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)  # 23:00 +02:00
    await normalize(
        sessions,
        sleep("AsleepCore", before, before + timedelta(hours=1, minutes=30)),  # ends 00:30
        sleep("AsleepDeep", before + timedelta(hours=1, minutes=30), before + timedelta(hours=3)),
        sleep("AsleepREM", before + timedelta(hours=3), before + timedelta(hours=9)),  # 07:00
        # One step sample per UTC hour of the local 25th: 25 of them.
        *(
            quantity(
                "StepCount",
                10,
                "count",
                datetime(2026, 10, 24, 22, 0, tzinfo=UTC) + timedelta(hours=index),
                datetime(2026, 10, 24, 22, 0, tzinfo=UTC) + timedelta(hours=index + 1),
            )
            for index in range(25)
        ),
    )
    days = await daily(sessions, change - timedelta(days=1), change)
    assert days[change]["sleep_seconds"] == 9 * 3600
    assert "sleep_seconds" not in days.get(change - timedelta(days=1), {})
    assert days[change]["steps"] == 250


async def test_resting_hrv_and_spo2_daily_values(sessions):
    await normalize(
        sessions,
        quantity("RestingHeartRate", 55, "count/min", at(6)),
        quantity("RestingHeartRate", 53, "count/min", at(20)),
        quantity("HeartRateVariabilitySDNN", 40, "ms", at(2)),
        quantity("HeartRateVariabilitySDNN", 50, "ms", at(4)),
        # The 02:00 sample again through Health Auto Export: one sample per timestamp.
        hae_metric(
            "heart_rate_variability", "ms", {"date": stamp(at(2)), "qty": 40, "source": WATCH}
        ),
        quantity("OxygenSaturation", "0.95", "%", at(3)),
        quantity("OxygenSaturation", "0.97", "%", at(5)),
    )
    values = (await daily(sessions))[DAY]
    assert values["resting_bpm"] == 53  # the newest of the day
    assert values["hrv_ms"] == 45
    assert values["spo2"] == 96


async def test_heart_rate_samples_one_per_timestamp(sessions):
    await normalize(
        sessions,
        quantity("HeartRate", 60, "count/min", at(8)),
        quantity("HeartRate", 62, "count/min", at(8, 1)),
        hae_metric("heart_rate", "count/min", {"date": stamp(at(8)), "Avg": 60, "source": WATCH}),
    )
    async with sessions() as session:
        samples = await apple_readers.heart_rate_samples(session, at(0), at(23))
    assert samples == [
        (datetime(2026, 11, 6, 7, 0, tzinfo=UTC), 60.0),
        (datetime(2026, 11, 6, 7, 1, tzinfo=UTC), 62.0),
    ]


async def test_a_workout_both_channels_carry_is_listed_once(sessions):
    await normalize(
        sessions,
        workout("Running", at(17), at(17, 30), duration="30", durationUnit="min"),
        hae_workout_record(
            {
                "id": "W-1",
                "name": "Outdoor Run",
                "start": stamp(at(17, 1)),
                "end": stamp(at(17, 30)),
                "duration": 1740,
            }
        ),
        hae_workout_record(
            {"id": "W-2", "name": "Yoga", "start": stamp(at(19)), "end": stamp(at(19, 45))}
        ),
    )
    async with sessions() as session:
        rows = await apple_readers.workouts(session, at(0), at(23))
    assert [row["exercise_type"] for row in rows] == ["Running", "Yoga"]
    assert all(row["source_code"] == "HEALTH_KIT" and row["provider"] == "APPLE" for row in rows)
    assert rows[0]["duration_seconds"] == 1800


async def test_sources_list_the_bookkeeping_rows(sessions):
    seen = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(
            AppleSource(
                source_name=WATCH,
                device="Apple Watch",
                first_seen_at=seen,
                last_seen_at=seen,
                last_data_at=seen - timedelta(hours=2),
                data_types=["HKQuantityTypeIdentifierStepCount"],
                channels=["export"],
                records=3,
            )
        )
    async with sessions() as session:
        (source,) = await apple_readers.sources(session)
    assert source["source_name"] == WATCH and source["device"] == "Apple Watch"
    assert source["last_data_at"] == seen - timedelta(hours=2) and source["records"] == 3


async def test_readers_see_only_apple_rows(sessions):
    from decimal import Decimal

    from app.models.health import Steps

    async with sessions.begin() as session:
        session.add(
            Steps(
                google_data_point_id="synthetic:g",
                start_time=at(7),
                end_time=at(8),
                count=Decimal(5),
                source_record_id=f"export:HKQuantityTypeIdentifierStepCount:{'0' * 64}",
            )
        )
    assert await daily(sessions) == {}
