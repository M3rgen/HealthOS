"""Source precedence: the policy, the resolver and the configuration behind them.

Synthetic observations only. The maintainer's rule (7 November 2026): sport activities show
Garmin first, every other health metric the Fitbit first; nothing is summed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.services import source_precedence as sp
from app.services.source_precedence import (
    ACTIVITY,
    HEALTH,
    MANUAL,
    PRIMARY,
    SECONDARY,
    UNRANKED,
    Observation,
    Policy,
    resolve,
    resolve_daily,
    resolve_samples,
    resolve_sessions,
    sample_category,
)

DEFAULT = Policy()
REVERSED = Policy(activity=("FITBIT", "GARMIN"), health=("GARMIN", "FITBIT"))
T = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T + timedelta(minutes=minutes)


def session(record_id: str, source: str, start: int, end: int) -> Observation:
    return Observation(source, {"id": record_id}, record_id=record_id, start=at(start), end=at(end))


def test_every_dashboard_metric_has_a_category_and_workouts_are_activity():
    assert sp.category("workouts") == ACTIVITY and sp.category("exercise") == ACTIVITY
    for metric in ("steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2", "heart_rate"):
        assert sp.category(metric) == HEALTH
    assert sp.category("something-new") == HEALTH  # unknown metrics are health, never activity


def test_policy_ranks_listed_sources_first_then_google_then_the_rest():
    assert DEFAULT.rank("GARMIN", ACTIVITY) == 0 and DEFAULT.rank("FITBIT", ACTIVITY) == 1
    assert DEFAULT.rank("FITBIT", HEALTH) == 0 and DEFAULT.rank("GARMIN", HEALTH) == 1
    assert DEFAULT.rank("GOOGLE", HEALTH) == 3 < DEFAULT.rank("WHOOP", HEALTH)
    assert DEFAULT.priority("GARMIN", ACTIVITY) == PRIMARY
    assert DEFAULT.priority("GARMIN", HEALTH) == SECONDARY
    assert DEFAULT.priority("WHOOP", HEALTH) == UNRANKED
    assert DEFAULT.as_dict() == {
        "activity": ["GARMIN", "FITBIT", "HEALTH_KIT"],
        "health": ["FITBIT", "GARMIN", "HEALTH_KIT"],
    }


def test_policy_comes_from_the_configuration_and_reverses_in_one_line():
    settings = Settings(_env_file=None, source_precedence_health="garmin, fitbit")
    policy = Policy.from_settings(settings)
    assert policy.health == ("GARMIN", "FITBIT")  # normalized to upper case, spaces dropped
    assert policy.activity == ("GARMIN", "FITBIT", "HEALTH_KIT")  # the default, untouched
    assert Policy.from_settings(Settings(_env_file=None)) == DEFAULT


@pytest.mark.parametrize("value", ["", " , ", "FITBIT,fitbit", "FIT BIT", "GARMIN;FITBIT"])
def test_configuration_rejects_an_empty_or_malformed_precedence(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, source_precedence_activity=value)


def test_daily_conflict_selects_by_policy_and_keeps_the_other_value_unsummed():
    both = {"FITBIT": 8000, "GARMIN": 7400}
    health = resolve_daily("steps", both, DEFAULT)
    assert health.value == 8000 and health.selected.source == "FITBIT"
    assert [item.value for item in health.alternatives] == [7400]
    assert health.as_dict() == {
        "source": "FITBIT",
        "label": "Fitbit",
        "alternatives": [{"source": "GARMIN", "label": "Garmin", "value": 7400}],
    }
    # Reversing the policy changes the selection, not the set of observations.
    reversed_ = resolve_daily("steps", both, REVERSED)
    assert reversed_.value == 7400 and [i.value for i in reversed_.alternatives] == [8000]
    assert {health.value, *(i.value for i in health.alternatives)} == set(both.values())
    # A source without a value is not an alternative; no value at all is no selection.
    only_garmin = resolve_daily("steps", {"FITBIT": None, "GARMIN": 7400}, DEFAULT)
    assert only_garmin.value == 7400 and only_garmin.alternatives == ()
    assert resolve_daily("steps", {}, DEFAULT).as_dict()["source"] is None


def test_activity_metrics_take_the_activity_order():
    both = [Observation("FITBIT", 1), Observation("GARMIN", 2, provider="GARMIN")]
    assert resolve("workouts", both, DEFAULT).selected.source == "GARMIN"
    assert resolve("steps", both, DEFAULT).selected.source == "FITBIT"
    assert resolve("workouts", both, REVERSED).selected.source == "FITBIT"


def test_google_source_labels():
    assert sp.google_source("FITBIT") == "FITBIT" and sp.google_source(None) == "GOOGLE"
    assert sp.google_source_of(["FITBIT", "FITBIT"]) == "FITBIT"
    assert sp.google_source_of(["FITBIT", "HEALTH_KIT"]) == "GOOGLE"  # mixed: unknown platform
    assert sp.label("HEALTH_KIT") == "Apple Sağlık" and sp.label("WHOOP") == "Whoop"
    assert sp.label(None) is None


def test_overlapping_sessions_form_one_group_with_the_best_source_primary():
    garmin_run = session("g1", "GARMIN", 0, 60)
    fitbit_auto = session("f1", "FITBIT", 10, 40)  # auto-detected inside the run
    fitbit_later = session("f2", "FITBIT", 50, 90)  # overlaps the run's tail
    fitbit_apart = session("f3", "FITBIT", 200, 230)  # its own group
    manual = session("m1", MANUAL, 0, 60)  # typed by the owner: never resolved
    placed = resolve_sessions(
        [fitbit_later, garmin_run, fitbit_auto, fitbit_apart, manual], DEFAULT
    )
    assert placed["g1"].priority == PRIMARY and placed["g1"].group is None
    assert [item.record_id for item in placed["g1"].alternatives] == ["f1", "f2"]
    assert placed["f1"] == sp.SessionPlacement(SECONDARY, "g1")
    assert placed["f2"] == sp.SessionPlacement(SECONDARY, "g1")
    assert placed["f3"].priority == PRIMARY and placed["f3"].alternatives == ()
    assert placed["m1"].priority == PRIMARY and placed["m1"].alternatives == ()
    # Reversed: the Fitbit sessions lead, the Garmin run is the alternative of the first one.
    reversed_ = resolve_sessions([garmin_run, fitbit_auto, fitbit_later], REVERSED)
    assert reversed_["f1"].priority == PRIMARY and reversed_["f2"].priority == PRIMARY
    assert reversed_["g1"] == sp.SessionPlacement(SECONDARY, "f1")
    assert [i.record_id for i in reversed_["f1"].alternatives] == ["g1"]


def test_sessions_of_one_source_never_conflict_and_touching_sessions_do_not_overlap():
    first, second = session("g1", "GARMIN", 0, 30), session("g2", "GARMIN", 10, 40)
    fitbit = session("f1", "FITBIT", 40, 60)  # starts when g2 ends: half-open, no overlap
    placed = resolve_sessions([first, second, fitbit], DEFAULT)
    assert all(item.priority == PRIMARY for item in placed.values())
    assert all(item.alternatives == () for item in placed.values())


def test_minute_samples_follow_the_health_policy_outside_and_activity_inside_a_workout():
    fitbit = [(at(0), 60.0), (at(1), 61.0), (at(2), 62.0)]
    garmin = [(at(0), 70.0), (at(1), 71.0), (at(2), 72.0), (at(3), 73.0)]
    window = [(at(2), at(4))]
    assert sample_category(at(2), window) == ACTIVITY and sample_category(at(1), window) == HEALTH
    kept, chosen = resolve_samples({"FITBIT": fitbit, "GARMIN": garmin}, window, DEFAULT)
    assert [(moment.minute, value, source) for moment, value, source in kept] == [
        (0, 60.0, "FITBIT"),
        (1, 61.0, "FITBIT"),
        (2, 72.0, "GARMIN"),  # inside the workout: activity policy
        (3, 73.0, "GARMIN"),  # only Garmin measured this minute
    ]
    assert chosen == {"FITBIT": 2, "GARMIN": 2}
    # No averaging across sources: every kept value is one source's own sample.
    assert all(value in {v for _, v in fitbit} | {v for _, v in garmin} for _, value, _ in kept)
    without_window, _ = resolve_samples({"FITBIT": fitbit, "GARMIN": garmin}, (), DEFAULT)
    assert [source for _, _, source in without_window] == ["FITBIT"] * 3 + ["GARMIN"]
