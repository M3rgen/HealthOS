"""Public normalization results and temporal fallbacks before splitting the dispatch."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.google_health.data_types import REGISTRY
from app.services.archive.normalization import normalize

START = datetime(2026, 9, 1, 12, tzinfo=UTC)
END = datetime(2026, 9, 1, 13, tzinfo=UTC)
SAMPLE = {"timestamp": START}
INTERVAL = {"start_time": START, "end_time": END}


@pytest.mark.parametrize(
    "kind, payload, model, values",
    [
        ("heart-rate", {"bpm": "62"}, "HeartRate", {**SAMPLE, "bpm": 62, "motion_context": None}),
        *(
            (kind, {"rmssd": "25"}, "HeartRateVariability", {**SAMPLE, "rmssd": 25})
            for kind in ("heart-rate-variability", "daily-heart-rate-variability")
        ),
        *(
            (kind, {"average": "97"}, "OxygenSaturation", {**SAMPLE, "spo2": 97})
            for kind in ("oxygen-saturation", "daily-oxygen-saturation")
        ),
        ("steps", {"countSum": "42"}, "Steps", {**INTERVAL, "count": 42}),
        (
            "exercise",
            {"metricsSummary": [], "activeDuration": "bad", "duration": "0s"},
            "ExerciseSession",
            {
                **INTERVAL,
                "exercise_type": None,
                "duration_seconds": 0,
                "calories_kcal": None,
                "distance_meters": None,
            },
        ),
        (
            "daily-resting-heart-rate",
            {"beatsPerMinute": "60", "dailyRestingHeartRateMetadata": []},
            "RestingHeartRate",
            {**SAMPLE, "bpm": 60, "calculation_method": None},
        ),
        *(
            (
                kind,
                {"runVo2Max" if kind == "run-vo2-max" else "vo2Max": "48.5"},
                "Vo2Max",
                {
                    **SAMPLE,
                    "metric_type": kind,
                    "ml_per_kg_min": Decimal("48.5"),
                    "fitness_level": None,
                },
            )
            for kind in ("vo2-max", "daily-vo2-max", "run-vo2-max")
        ),
        *(
            (
                kind,
                {"nightlyTemperatureCelsius": "36.5"},
                "Temperature",
                {
                    **SAMPLE,
                    "metric_type": kind,
                    "celsius": Decimal("36.5"),
                    "baseline_celsius": None,
                },
            )
            for kind in ("core-body-temperature", "daily-sleep-temperature-derivations")
        ),
        *(
            (
                kind,
                {"fullSleepStats": {"breathsPerMinute": "12"}},
                "RespiratoryRate",
                {**SAMPLE, "metric_type": kind, "breaths_per_minute": 12},
            )
            for kind in ("daily-respiratory-rate", "respiratory-rate-sleep-summary")
        ),
        (
            "active-zone-minutes",
            {"activeZoneMinutes": "0"},
            "ActiveZoneMinute",
            {**INTERVAL, "minutes": 0, "heart_rate_zone": None},
        ),
        ("distance", {"millimeters": "1500"}, "Distance", {**INTERVAL, "meters": Decimal("1.5")}),
        *(
            (kind, {"kcalSum": "1.25"}, "EnergyExpenditure", {**INTERVAL, "kcal": Decimal("1.25")})
            for kind in ("active-energy-burned", "total-calories")
        ),
        ("weight", {"weightGrams": "72500"}, "Weight", {**SAMPLE, "kilograms": Decimal("72.5")}),
    ],
)
def test_dedicated_dispatch_preserves_complete_values(kind, payload, model, values):
    payload = {"interval": {"startTime": START.isoformat(), "endTime": END.isoformat()}, **payload}
    result = normalize(
        REGISTRY[kind], {"name": "synthetic-dispatch", REGISTRY[kind].json_key: payload}
    )
    assert result is not None and result.model.__name__ == model
    if model == "HeartRateVariability":
        values = {**values, "metrics": payload}
    assert result.values == {
        "google_data_point_id": "synthetic-dispatch",
        "device": None,
        "source_timezone": None,
        **values,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "steps",
        "sleep",
        "exercise",
        "active-zone-minutes",
        "distance",
        "active-energy-burned",
        "total-calories",
    ],
)
def test_interval_without_end_still_uses_generic_fallback(kind):
    payload = {"interval": {"startTime": START.isoformat()}, "value": "7", "unit": "sample"}
    result = normalize(
        REGISTRY[kind], {"name": "synthetic-dispatch", REGISTRY[kind].json_key: payload}
    )
    assert result is not None and result.model.__name__ == "ScalarMetric"
    assert result.values == {
        "google_data_point_id": "synthetic-dispatch",
        "device": None,
        "source_timezone": None,
        "data_type": kind,
        "start_time": START,
        "end_time": None,
        "value": 7,
        "unit": "sample",
        "attributes": payload,
    }
