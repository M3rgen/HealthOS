"""The raw daily API projection preserves the normalization fallback choices."""

import pytest

from app.services.providers.garmin_pull import _day

TOKENS = [
    ("-1e-1000", None),
    ("-1e-10000", None),
    ("0", 0),
    ("-0.0", -0.0),
    ("-0e-1000", -0.0),
    ("1e-1000", 0.0),
    ("12.25", 12.25),
    ("1000000000000000000000000000000", 10**30),
]


@pytest.mark.parametrize("token,expected", TOKENS)
@pytest.mark.parametrize("primary", [False, True])
async def test_sql_numeric_tokens_keep_sign_before_daily_field_validation(
    sessions, token, expected, primary
):
    import json
    import math

    from sqlalchemy import String, cast, select
    from test_garmin_normalize import DAY
    from test_garmin_step_validity import insert_raw_json

    from app.models.garmin import GarminRawRecord
    from app.services.providers import garmin_daily_summary as reader

    async with sessions.begin() as session:
        for kind, paths in reader.FIELDS.items():
            body = {}
            for path in paths:
                target = body
                for name in path[:-1]:
                    target = target.setdefault(name, {})
                target[path[-1]] = "TOKEN"
            # A negative primary cannot be rescued by these positive alternates.
            alternate = primary and kind in ("heart-rate", "hrv", "sleep-summaries")
            text = json.dumps(body).replace('"TOKEN"', "60" if alternate else token)
            if primary or kind not in ("daily-summaries", "hrv-summaries", "sleep"):
                await insert_raw_json(session, kind, text)
    async with sessions() as session:
        day = (await reader.daily_summaries(session, DAY, DAY))[0]
        archived = await session.scalar(
            select(cast(GarminRawRecord.body.op("->")("totalSteps"), String)).where(
                GarminRawRecord.kind == "steps"
            )
        )
        postgres = session.get_bind().dialect.name == "postgresql"
    values = [
        day[key]
        for key in (
            "steps",
            "resting_heart_rate",
            "hrv_last_night_average",
            "spo2_average",
            "sleep_seconds",
        )
    ] + list(day["sleep_stages_seconds"].values())
    assert values == [expected] * 9
    assert all(type(value) is type(expected) for value in values)
    if expected == 0:
        # jsonb numeric has no negative zero: PostgreSQL archives -0.0 as 0.0. The API
        # returns the zero's sign exactly as archived, on either backend.
        signed = archived.startswith("-")
        assert signed == (token.startswith("-") and not postgres)
        assert all(math.copysign(1, value) == (-1 if signed else 1) for value in values)
    # Distance, stress and score retain their existing unrestricted numeric policy.
    for value in (day["distance_m"], day["stress_average"], day["sleep_score"]):
        assert value == json.loads(token)
        assert type(value) is type(json.loads(token))


@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.0001, None),
        (-1e-300, None),
        (0, 0),
        (-0.0, -0.0),
        (12.25, 12.25),
        (10**30, 10**30),
        ("12", None),
        (True, None),
        (None, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_steps_keep_raw_api_types_and_bounds_while_rejecting_negatives(value, expected):
    day = _day("2026-11-06", {"steps": {"totalSteps": value, "totalDistance": 5400}})
    assert day["steps"] == expected
    assert day["distance_m"] == 5400


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


@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.25, None),
        (-0.0001, None),
        (-1e-300, None),
        ("-1e-10000", None),
        (0, 0),
        (-0.0, -0.0),
        (12.25, 12.25),
        (10**30, 10**30),
        ("12", None),
        (True, None),
        (None, None),
    ],
)
def test_daily_scalar_api_preserves_types_and_does_not_rescue_negative_primary(value, expected):
    day = _day(
        "2026-11-06",
        {
            "daily-summaries": {"heartRate": {"restingValue": value}},
            "heart-rate": {"restingHeartRate": 60},
            "hrv-summaries": {"lastNightAvg": value},
            "hrv": {"hrvSummary": {"lastNightAvg": 40}},
            "spo2": {"averageSpO2": value},
        },
    )
    # The raw API has always fallen back from a nonnumeric resting field.
    resting = 60 if value is None or isinstance(value, str | bool) else expected
    assert day["resting_heart_rate"] == resting
    assert day["hrv_last_night_average"] == expected
    assert day["spo2_average"] == expected


@pytest.mark.parametrize("kind", ["sleep", "sleep-summaries"])
@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.0001, None),
        (-1e-300, None),
        ("-1e-10000", None),
        (0, 0),
        (-0.0, -0.0),
        ("-0e-10000", None),
        (12.25, 12.25),
        ("12.25", None),
        (True, None),
        (None, None),
        (float("inf"), None),
    ],
)
def test_sleep_api_duration_fields_reject_negatives_but_preserve_type_policy(kind, value, expected):
    fields = (
        "sleepTimeSeconds",
        "deepSleepSeconds",
        "lightSleepSeconds",
        "remSleepSeconds",
        "awakeSleepSeconds",
    )
    primary = dict.fromkeys(fields, value) | {"sleepScores": {"overall": {"value": 80}}}
    records = {"sleep-summaries": dict.fromkeys(fields, 100), kind: primary}
    day = _day("2026-11-06", records)
    assert day["sleep_seconds"] == expected
    assert type(day["sleep_seconds"]) is type(expected)
    assert day["sleep_stages_seconds"] == dict.fromkeys(("deep", "light", "rem", "awake"), expected)
    assert day["sleep_score"] == 80
    # Each invalid field is independent; other fields in the chosen record remain readable.
    for field in fields:
        day = _day("2026-11-06", {kind: {**dict.fromkeys(fields, 100), field: value}})
        assert sum(v == 100 for v in day["sleep_stages_seconds"].values()) == (
            4 if field == fields[0] else 3
        )


@pytest.mark.parametrize(
    "value",
    [None, False, True, {}, [], [1], "", "12", 0, -0.0, -1, -1e-300, 12.25, 10**30],
)
@pytest.mark.parametrize("mode", ["primary", "fields", "containers"])
async def test_sql_projection_preserves_the_raw_day_contract(sessions, value, mode):
    from garmin_records import daily_summary, sleep
    from test_garmin_normalize import DAY, raw

    from app.services.providers import garmin_daily_summary as reader

    records = {
        "steps": {"totalSteps": value, "totalDistance": value},
        "daily-summaries": daily_summary(DAY),
        "heart-rate": {"restingHeartRate": 60},
        "stress": {"values": {"overallStressLevel": value}},
        "sleep": value,
        "sleep-summaries": sleep(DAY),
        "hrv-summaries": value,
        "hrv": {"hrvSummary": {"lastNightAvg": 40}},
        "spo2": {"averageSpO2": value},
    }
    if mode == "fields":
        records["sleep"] = {path[0]: value for path in reader.FIELDS["sleep"]}
        records["sleep"]["sleepScores"] = {"overall": {"value": value}}
        records["hrv-summaries"] = {"lastNightAvg": value}
        records["daily-summaries"] = {
            "heartRate": {"restingValue": value},
            "stress": {"avgLevel": 30},
            "bodyBattery": dict.fromkeys(
                ("maxValue", "minValue", "latestValue", "chargedValue", "drainedValue"), value
            ),
        }
    elif mode == "containers":
        records["daily-summaries"] = dict.fromkeys(("heartRate", "bodyBattery", "stress"), value)
        records["sleep"] = {"sleepScores": value}
        records["hrv"] = {"hrvSummary": value}
        records["hrv-summaries"] = {}
        records["stress"] = {"values": value}
    async with sessions.begin() as session:
        session.add_all(raw(kind, str(DAY), body) for kind, body in records.items())
    async with sessions() as session:
        actual = await reader.daily_summaries(session, DAY, DAY)
    expected = _day(str(DAY), records)
    assert actual == [expected]
    for key, value in actual[0].items():
        assert type(value) is type(expected[key])
        if isinstance(value, dict):
            assert {k: type(v) for k, v in value.items()} == {
                k: type(v) for k, v in expected[key].items()
            }
