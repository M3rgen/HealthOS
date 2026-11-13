"""Shared Personal API payloads must remain visibly synthetic, including new fields."""

from datetime import date, datetime

import garmin_records as records
import pytest

DAY = date(2000, 1, 1)
# API identifiers and descriptor positions are not measurements; their formats are fixed.
METADATA = {"id", "activityId", "userProfilePk", "userProfilePK", "typeId", "parentTypeId", "index"}
FACTORIES = (
    records.daily_summary,
    records.daily_steps,
    records.daily_stress,
    records.heart_rate,
    records.sleep,
    records.hrv_summary,
    lambda day: records.activity(1000, day),
)


def numeric_fields(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in METADATA:
                yield from numeric_fields(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from numeric_fields(item, (*path, index))
    elif type(value) in (int, float):
        yield path, value


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda factory: factory.__name__)
def test_shared_health_values_follow_synthetic_pattern(factory):
    body = factory(DAY)
    fields = list(numeric_fields(body))
    assert fields
    for path, value in fields:
        # Millisecond stamps use the deliberately artificial epoch, in whole minutes.
        if "TimestampGMT" in str(path[-1]) or (path[0] == "heartRateValues" and path[-1] == 0):
            assert (value - 1_700_000_000_000) % 60_000 == 0, path
        elif type(value) is int:
            assert value % 10 == 0, path
        else:
            assert value * 2 == int(value * 2), path
    for field in ("createTimeStamp", "startTimeLocal", "startTimeGMT"):
        if field in body:
            stamp = datetime.fromisoformat(body[field])
            assert stamp.minute == stamp.second == stamp.microsecond == 0, field


def test_shared_health_values_keep_totals_bounds_and_distinct_samples():
    summary, heart, night = records.daily_summary(DAY), records.heart_rate(DAY), records.sleep(DAY)
    calories = summary["calories"]
    assert calories["burnedTotal"] == calories["burnedActive"] + calories["burnedResting"]
    assert heart["minHeartRate"] <= heart["restingHeartRate"] <= heart["maxHeartRate"]
    assert len({bpm for _, bpm in heart["heartRateValues"]}) == 2
    stages = ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds", "awakeSleepSeconds")
    assert night["sleepTimeSeconds"] == sum(night[field] for field in stages)
    assert night["sleepEndTimestampGMT"] - night["sleepStartTimestampGMT"] == (
        night["sleepTimeSeconds"] * 1000
    )
