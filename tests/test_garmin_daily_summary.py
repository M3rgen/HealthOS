"""The raw daily API projection preserves the normalization fallback choices."""

import pytest

from app.services.providers.garmin_pull import _day


@pytest.mark.parametrize("value", [None, True, "invalid", {}, float("nan"), float("inf"), 0, 24])
@pytest.mark.parametrize("metric", ["resting_heart_rate", "stress_average"])
def test_number_priority_and_invalid_preferred_fallback(metric, value):
    records = {
        "daily-summaries": {"heartRate": {"restingValue": value}, "stress": {"avgLevel": 30}},
        "heart-rate": {"restingHeartRate": 60},
        "stress": {"values": {"overallStressLevel": value}},
    }
    expected = value if type(value) is int else (60 if metric == "resting_heart_rate" else 30)
    assert _day("2026-11-06", records)[metric] == expected


@pytest.mark.parametrize(
    "primary,sleep_seconds,hrv_ms",
    [
        (None, 100, 40),
        ({}, 100, 40),
        ({"calendarDate": "2026-11-06"}, None, None),
        ({"sleepTimeSeconds": True, "lastNightAvg": True}, None, None),
        ({"sleepTimeSeconds": 0, "lastNightAvg": 0}, 0, 0),
        ({"sleepTimeSeconds": 10, "lastNightAvg": 20}, 10, 20),
    ],
)
def test_sleep_and_hrv_fallback_depends_on_whole_primary_record(primary, sleep_seconds, hrv_ms):
    day = _day(
        "2026-11-06",
        {
            "sleep": primary,
            "sleep-summaries": {"sleepTimeSeconds": 100, "deepSleepSeconds": 20},
            "hrv-summaries": primary,
            "hrv": {"hrvSummary": {"lastNightAvg": 40}},
        },
    )
    assert day["sleep_seconds"] == sleep_seconds
    assert day["sleep_stages_seconds"]["deep"] == (None if primary else 20)
    assert day["hrv_last_night_average"] == hrv_ms


def test_missing_or_malformed_alternates_leave_nulls():
    for records in ({}, {"heart-rate": [], "stress": {}, "hrv": {"hrvSummary": False}}):
        day = _day("2026-11-06", records)
        for metric in (
            "resting_heart_rate",
            "stress_average",
            "sleep_seconds",
            "hrv_last_night_average",
        ):
            assert day[metric] is None
