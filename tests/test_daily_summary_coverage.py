"""Current daily summary corrections retain invalid source evidence in coverage."""

from datetime import timedelta

import pytest
from derived_records import DAY, PHONE, T0, read, snapshot, store, streams, summary
from interpretation_records import QUERY, call
from support.coverage_fixtures import api as api
from support.coverage_fixtures import coverage_of, fetch
from test_google_platform_daily import METRICS, project

RMSSD = "rootMeanSquareOfSuccessiveDifferencesMilliseconds"


@pytest.mark.parametrize("kind,key,field,model", METRICS[2:])
@pytest.mark.parametrize("negative", [-1, "-1", "-1e-999999", "-1e999999"])
async def test_negative_summary_is_invalid_at_its_source_and_recovers(
    sessions, api, kind, key, field, model, negative
):
    for value in (60, negative, 0, "0", "-0", "-0e999999", "1e-100", "1e100"):
        await fetch(
            sessions, T0, T0 + timedelta(days=1), kind, points=[summary(kind, **{field: value})]
        )
        async with sessions() as session:
            before = await snapshot(session)
        response = await api.get("/api/sync/coverage", params={**QUERY, "data_type": kind})
        assert response.status_code == 200, response.text
        report = response.json()["types"][0]
        umay = coverage_of(await call(sessions, "coverage", data_type=kind), key)
        assert report["days"][0]["has_value"] is (value != negative)
        assert report["days"][0]["retrieval"] == "completed_with_records"
        assert umay["value_days"] == umay["sampling"]["sample_count"] == int(value != negative)
        assert (
            umay["exclusions"]["invalid_shape"]
            == report["exclusions"]["invalid_shape"]
            == int(value == negative)
        )
        async with sessions() as session:
            assert await snapshot(session) == before
    fields = {
        "hrv_ms": ("averageHeartRateVariabilityMilliseconds", "rmssd"),
        "spo2": ("averagePercentage", "percentage"),
        "resting_bpm": (field,),
    }[key]
    await store(
        sessions,
        kind,
        summary(kind, **{f: negative if i == 0 else 91 for i, f in enumerate(fields)}),
        summary(kind, "phone", source=PHONE, **{fields[0]: 80}),
    )
    metric = coverage_of(await call(sessions, "coverage", data_type=kind), key)
    assert metric["exclusions"]["invalid_shape"] == 1
    assert metric["sampling"]["sample_count"] == metric["value_days"] == 1
    assert [r["platform"] for r in metric["source_regimes"]] == ["HEALTH_KIT"]


@pytest.mark.parametrize(
    "kind,key,primary,alias",
    [
        ("daily-oxygen-saturation", "spo2", "averagePercentage", "average"),
        ("daily-heart-rate-variability", "hrv_ms", RMSSD, "rmssd"),
    ],
)
@pytest.mark.parametrize(
    "first", [-5, "-1e-999999", "NaN", "sNaN", "-Infinity", "1e1000", 91, "broken", "missing"]
)
async def test_an_alias_never_rescues_a_rejected_field_in_any_reader(
    sessions, monkeypatch, kind, key, primary, alias, first
):
    """The dashboard, coverage, the canonical day and Umay judge one record alike.

    A rejected number (negative or nonfinite) keeps the alias out; only a missing
    or non-numeric field falls through to it (maintainer, 2026-11-13)."""
    fields = {primary: first, alias: 91 if first == 91 else 95}
    if first == "missing":
        del fields[primary]
    await store(sessions, kind, summary(kind, **fields))
    expected = {91: 91, "broken": 95, "missing": 95}.get(first)
    day, _ = await project(sessions, monkeypatch)
    umay = await call(sessions, "baseline", metric=key, end_date=str(DAY + timedelta(days=1)))
    [stream] = streams(await read(sessions), key)
    assert day[key] == stream["value"] == umay["summaries"][0]["value"] == expected
    observed = sorted(o["field"] for o in stream["observations"])
    assert observed == sorted(
        [alias if first in ("missing", "broken") else primary]
        if key == "spo2"
        else [alias]
        if first == "missing"
        else [primary, alias][: 1 + (first in (91, "broken"))]
    )
    metric = coverage_of(await call(sessions, "coverage", data_type=kind), key)
    assert metric["value_days"] == int(expected is not None)
    assert metric["exclusions"]["invalid_shape"] == int(expected is None)
