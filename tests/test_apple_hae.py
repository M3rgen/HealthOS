"""Health Auto Export request bodies: documented shapes, identity keys and refusals.

Synthetic bodies built from the documented field names (tests/apple_fixtures.py); no real
device payload was available.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from apple_fixtures import (
    PHONE,
    WATCH,
    hae_blood_pressure,
    hae_body,
    hae_heart_rate,
    hae_metric,
    hae_sleep_stages,
    hae_sleep_summary,
    hae_steps,
    hae_workout_v1,
    hae_workout_v2,
)

from app.apple.hae import HaeFormatError, HaeResult, parse_hae
from app.apple.records import HAE, HAE_METRIC, HAE_WORKOUT, identity_key

HEADERS = {"Automation-Aggregation": "hours", "automation-name": "Test automation"}


def test_generic_quantity_metric():
    result = parse_hae(hae_body(metrics=[hae_steps(1234)]), HEADERS)
    assert isinstance(result, HaeResult)
    (item,) = result.records
    assert (item.channel, item.kind, item.type) == (HAE, HAE_METRIC, "step_count")
    assert item.body == {
        "metric": "step_count",
        "units": "count",
        "aggregation": "hours",
        "point": {"date": "2026-11-06 00:00:00 +0100", "qty": 1234, "source": WATCH},
    }
    assert item.start == datetime(2026, 11, 5, 23, 0, tzinfo=UTC) and item.end is None
    assert item.source_name == WATCH and item.device is None
    assert item.key == identity_key(
        "step_count", "hours", "2026-11-06 00:00:00 +0100", *[None] * 6, WATCH, None
    )
    assert result.counts == {HAE_METRIC: 1} and result.skipped == {} and result.invalid == 0


def test_heart_rate_blood_pressure_and_an_unknown_metric_keep_their_points():
    unknown = hae_metric(
        "future_metric_name", "zz", {"date": "2026-11-06 09:00:00 +0100", "qty": 3}
    )
    body = hae_body(metrics=[hae_heart_rate(), hae_blood_pressure(), unknown])
    records = parse_hae(body).records
    heart, pressure, future = records
    assert heart.body["point"]["Min"] == 55 and heart.body["point"]["Max"] == 120
    assert heart.body["units"] == "count/min" and heart.body["aggregation"] is None
    assert pressure.body["point"]["systolic"] == 120 and pressure.source_name == PHONE
    assert future.type == "future_metric_name" and future.body["point"]["qty"] == 3
    assert future.source_name is None


def test_sleep_summarized_and_unsummarized():
    summary = parse_hae(hae_body(metrics=[hae_sleep_summary()])).records[0]
    # sleepStart wins over date; the end is sleepEnd.
    assert summary.start == datetime(2026, 11, 5, 22, 10, tzinfo=UTC)
    assert summary.end == datetime(2026, 11, 6, 5, 55, tzinfo=UTC)
    assert summary.body["point"]["deep"] == 1.25
    stages = parse_hae(hae_body(metrics=[hae_sleep_stages()])).records
    assert [item.body["point"]["value"] for item in stages] == ["Core", "Deep", "REM"]
    assert stages[0].start == datetime(2026, 11, 5, 22, 10, tzinfo=UTC)
    assert stages[0].end == datetime(2026, 11, 6, 0, 10, tzinfo=UTC)
    assert len({item.key for item in stages}) == 3


def test_stage_value_is_part_of_the_identity():
    def stage(value):
        point = {"startDate": "2026-11-06 01:00:00 +0100", "endDate": "x", "value": value}
        return parse_hae(hae_body(metrics=[hae_metric("sleep_analysis", "hr", point)]))

    assert stage("Core").records[0].key != stage("Deep").records[0].key


def test_a_resent_point_keeps_its_key_and_a_changed_qty_changes_only_the_digest():
    first = parse_hae(hae_body(metrics=[hae_steps(1000)]), HEADERS).records[0]
    again = parse_hae(hae_body(metrics=[hae_steps(1000)]), HEADERS).records[0]
    later = parse_hae(hae_body(metrics=[hae_steps(2500)]), HEADERS).records[0]
    assert first == again and first.digest == again.digest
    assert later.key == first.key and later.digest != first.digest
    other_bucket = parse_hae(
        hae_body(metrics=[hae_steps(1000)]), {"automation-aggregation": "days"}
    )
    assert other_bucket.records[0].key != first.key


def test_workouts_v1_and_v2():
    result = parse_hae(hae_body(workouts=[hae_workout_v2(), hae_workout_v1()]))
    v2, v1 = result.records
    assert (v2.kind, v2.type, v2.body["version"]) == (HAE_WORKOUT, "Outdoor Run", "2")
    assert v2.key == identity_key("id", "11111111-2222-3333-4444-555555555555")
    assert v2.source_name == WATCH
    assert v2.start == datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    assert v2.end == datetime(2026, 11, 6, 16, 30, 30, tzinfo=UTC)
    assert v2.body["workout"] == hae_workout_v2()
    assert (v1.type, v1.body["version"], v1.source_name) == ("Walking", "1", None)
    assert v1.key == identity_key(
        "Walking", "2026-11-06 12:00:00 +0100", "2026-11-06 12:40:00 +0100"
    )
    assert result.counts == {HAE_WORKOUT: 2}
    # The same workout id with changed content: same key, another version.
    changed = {**hae_workout_v2(), "duration": 1900}
    again = parse_hae(hae_body(workouts=[changed])).records[0]
    assert again.key == v2.key and again.digest != v2.digest
    string_source = {**hae_workout_v1(), "source": PHONE}
    assert parse_hae(hae_body(workouts=[string_source])).records[0].source_name == PHONE


def test_other_arrays_are_counted_as_skipped():
    body = hae_body(
        metrics=[hae_steps()],
        stateOfMind=[{"id": "a", "kind": "momentaryEmotion"}],
        ecg=[{"classification": "sinusRhythm"}, {"classification": "x"}],
        medications=[],
        symptoms=[{"name": "Headache"}],
        cycleTracking=[{}],
        heartRateNotifications=[{}],
        somethingNew={"a": 1},
    )
    result = parse_hae(body)
    assert len(result.records) == 1
    assert result.skipped == {
        "stateOfMind": 1,
        "ecg": 2,
        "medications": 0,
        "symptoms": 1,
        "cycleTracking": 1,
        "heartRateNotifications": 1,
        "somethingNew": 1,
    }


def test_junk_items_are_counted_invalid_never_raised():
    body = hae_body(
        metrics=[
            "nope",
            {"units": "count", "data": [{"qty": 1}]},
            {"name": "", "data": []},
            {"name": "step_count", "data": "x"},
            {"name": "step_count", "units": "count", "data": [1, None, {"qty": 1}]},
        ],
        workouts=[[], {"start": "2026-11-06 12:00:00 +0100"}, {"name": "Yoga"}],
    )
    result = parse_hae(body)
    assert result.invalid == 4 + 2 + 2
    # A point with no date still becomes a record, so nothing is lost.
    metric, workout = result.records
    assert metric.type == "step_count" and metric.start is None
    assert workout.type == "Yoga" and workout.start is None
    assert parse_hae(hae_body(metrics={"not": "a list"})).invalid == 1
    assert parse_hae(b'{"data": {}}').records == []


@pytest.mark.parametrize(
    "body, match",
    [
        (b"not json", "not valid JSON"),
        (b'{"data": {"metrics": [{"name": "x", "data": [{"qty": NaN}]}]}}', "constant"),
        (b'{"data": {"metrics": [], "x": Infinity}}', "constant"),
        (b'{"data": {}, "data": {}}', "duplicate"),
        (b"[]", "not_object"),
        (b'{"metrics": []}', "no 'data' object"),
        (b'{"data": []}', "no 'data' object"),
        (b'{"data": {"a": "\xff"}}', "UTF-8"),
        ('{"data": {}}'.encode("utf-16"), "UTF-8"),
        (b"", "not valid JSON"),
    ],
)
def test_invalid_bodies_are_refused(body, match):
    with pytest.raises(HaeFormatError, match=match):
        parse_hae(body)


def test_non_ascii_and_a_top_level_error_field_are_accepted():
    body = json.dumps(
        {
            "error": None,
            "data": {"metrics": [hae_metric("step_count", "count", {"source": "Test Saati Ü"})]},
        },
        ensure_ascii=False,
    ).encode()
    assert parse_hae(body).records[0].source_name == "Test Saati Ü"
