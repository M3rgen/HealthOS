"""Current raw step corrections through ingestion and production interpretation."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from derived_records import DAY, T0, read, snapshot, steps, store, streams, summary
from interpretation_records import call

from app.models.derived import DerivedDay
from app.services.interpretation.canonical_daily import METHOD


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("field", ["count", "countSum", "steps"])
@pytest.mark.parametrize("supplied", ["1e100", "-1e100", "1e-100", -1, "-1e-1000", "-0"])
async def test_current_steps_replace_old_value_and_day(sessions, initial, offset, field, supplied):
    if not initial:
        await store(sessions, "steps", steps())
    last = str(DAY + timedelta(days=1))
    await call(sessions, "daily", end_date=last)
    for count in (supplied, 0):
        record = steps(count=count, start=T0 + timedelta(days=offset))
        record["steps"][field] = record["steps"].pop("count")
        stats = await store(sessions, "steps", record)
        expected = None if Decimal(str(count)) < 0 else float(count)
        assert stats.normalization_skipped == int(count != 0 and supplied not in {"1e-100", "-0"})
        async with sessions() as session:
            before = await snapshot(session)
        card = (await call(sessions, "daily", end_date=last))["derived"]
        assert not offset or streams(card, "steps", 0) == []
        row = streams(card, "steps", offset)[0]
        assert row["value"] == row["observations"][0]["value"] == expected
        if expected is None:
            assert row["state"] == "no_value"
            assert {"no_value", "missing_observations"} <= set(row["uncertainties"])
        assert row["observations"][0]["start"] == record["steps"]["interval"]["startTime"]
        for action in ("baseline", "analyze"):
            result = await call(sessions, action, metric="steps", end_date=last)
            assert [r["value"] for r in result["summaries"]] == [expected]
        async with sessions() as session:
            assert await snapshot(session) == before


@pytest.mark.parametrize(
    "kind,metric,field",
    [
        ("steps", "steps", "count"),
        ("daily-resting-heart-rate", "resting_bpm", "beatsPerMinute"),
        ("daily-heart-rate-variability", "hrv_ms", "rmssd"),
        ("daily-oxygen-saturation", "spo2", "averagePercentage"),
    ],
)
async def test_negative_values_rebuild_previous_method_without_changing_its_evidence(
    sessions, kind, metric, field
):
    await store(
        sessions, kind, steps(count=-10) if kind == "steps" else summary(kind, **{field: -10})
    )
    card = await read(sessions)
    legacy = deepcopy(card["days"][0]["result"])
    legacy["method_version"] = "source-daily/8"
    legacy["streams"][0].update(method_version="source-daily/8", value=-10, state="estimated")
    legacy["streams"][0]["observations"][0]["value"] = -10
    async with sessions.begin() as session:
        row = await session.get(DerivedDay, (DAY, "UTC", METHOD))
        row.method, row.result = "source-daily/8", legacy
        await session.flush()
        before = await snapshot(session)
    rebuilt = await read(sessions)
    assert rebuilt["method_version"] == METHOD != "source-daily/8"
    assert streams(rebuilt, metric)[0]["value"] is None
    assert metric in rebuilt["days"][0]["result"]["missing_metrics"]
    async with sessions() as session:
        assert (await session.get(DerivedDay, (DAY, "UTC", "source-daily/8"))).result == legacy
        assert (await session.get(DerivedDay, (DAY, "UTC", METHOD))).result != legacy
        assert await snapshot(session) == before
