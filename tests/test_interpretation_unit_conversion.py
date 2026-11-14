"""Final-unit numeric validation through the production reader and raw corrections."""

import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from derived_records import PHONE, T0, point, snapshot, store
from interpretation_records import call, exercise
from profile_records import CASES, measurement
from sqlalchemy import select
from support.workouts import METRIC_VALUES, OPTIONAL_METRICS
from support.workouts import exercise as workout
from weight_records import NEGATIVE_GRAMS

from app.google_health.data_types import REGISTRY
from app.models import health
from app.models.health import Distance, EnergyExpenditure, ExerciseSession, ScalarMetric, Weight
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.common import payload_hash
from app.services.interpretation.interpretation_measures import PHYSIOLOGY
from app.services.interpretation.interpretation_types import Interpretation

NUMERIC = "numeric_overflow:unit_conversion"
MISSING = "unsupported_or_missing_value"


@pytest.mark.parametrize("corrected", [False, True], ids=["new", "correction"])
@pytest.mark.parametrize(
    "boundary,above",
    [
        ("3600", "3600.0000000000000001"),
        ("0.3", "0.30000000000000001"),
        ("86400.000001", "86400.000001000000001"),
        ("0.000001", "0.00000100000000000000001"),
    ],
)
@pytest.mark.parametrize(
    "active,expected,invalid",
    [
        ("boundary", "boundary", False),
        ("above", None, True),
        *[(v, None, True) for v in ("-1e-999999s", "-5e-324", "1e999999s")],
        *[(v, 0, False) for v in ("0s", "-0s", "-0e-999999s")],
        *[(v, None, False) for v in (..., None, "bad", {}, [], True, "1e-999999s")],
        *[(v, None, False) for v in ("NaNs", "sNaNs", "Infinitys", "-Infinitys")],
    ],
)
async def test_session_source_duration_precision_and_corrections(
    sessions, corrected, boundary, above, active, expected, invalid
):
    end = T0 + timedelta(seconds=float(boundary))
    interval = {"startTime": T0.isoformat(), "endTime": end.isoformat()}
    original = workout(start=T0, interval=interval, activeDuration=boundary + "s")
    neighbor = workout("neighbor", start=T0)
    neighbor["dataSource"] = PHONE
    cursor = None
    history = []
    previous = original if corrected else None
    if corrected:
        await store(sessions, "exercise", original, neighbor)
        cursor = (await call(sessions, "sessions", limit=1))["next_cursor"]
    if active == "boundary":
        active, expected = boundary + "s", float(boundary)
    elif active == "above":
        active = above + "s"
        assert float(above) == float(boundary)
    current = deepcopy(original)
    if active is ...:
        del current["exercise"]["activeDuration"]
    else:
        current["exercise"]["activeDuration"] = active
    for record, value, rejected in (
        (current, expected, invalid),
        (original, float(boundary), False),
    ):
        if previous is not None and previous != record:
            history.append(previous)
        written = await store(sessions, "exercise", record, neighbor)
        async with sessions() as session:
            before = await snapshot(session)
            raw = await session.scalar(
                select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
            )
            assert raw.raw_json == record
            versions = (
                await session.scalars(
                    select(RawHealthDataVersion).order_by(RawHealthDataVersion.version)
                )
            ).all()
            assert [v.raw_json for v in versions] == history
            if corrected and written.normalization_skipped:
                retained = await session.scalar(
                    select(ExerciseSession).where(
                        ExerciseSession.google_data_point_id == original["name"]
                    )
                )
                assert retained.duration_seconds == Decimal(boundary).quantize(Decimal("0.001"))
        result = await call(sessions, "sessions")
        Interpretation.model_validate(result)
        json.dumps(result, allow_nan=False)
        row = next(r for r in result["summaries"] if r["evidence"][0]["record"] == original["name"])
        assert row["reported_active_seconds"] == value
        assert ("invalid_reported_active_duration" in row["reason_codes"]) == rejected
        assert row["interval_seconds"] == float(boundary)
        assert row["start"] == T0.isoformat() and row["end"] == end.isoformat()
        assert row["calories_kcal"] == 123 and row["distance_meters"] == 4567
        assert row["exercise_type"] == "RUNNING" and row["continuous_running_seconds"] is None
        assert row["overlaps"] == [neighbor["name"]]
        assert "overlapping_sessions" in row["reason_codes"]
        assert row["source"] == "kaynak-" + payload_hash(original["dataSource"])
        if cursor and record != previous:
            assert (await call(sessions, "sessions", cursor=cursor))["code"] == "stale_cursor"
        cursor = (await call(sessions, "sessions", limit=1))["next_cursor"]
        previous = record
        async with sessions() as session:
            assert await snapshot(session) == before


@pytest.mark.parametrize("corrected", [False, True], ids=["new", "correction"])
@pytest.mark.parametrize("value", [-60, 7200, 3600.001, 0, 3500, 3600])
@pytest.mark.parametrize("form", [float, str, "{}s".format], ids=["number", "text", "seconds"])
async def test_session_active_duration_bounds_and_recovery(sessions, corrected, value, form):
    original = workout(start=T0)
    if corrected:
        await store(sessions, "exercise", original)
    current = workout(start=T0, activeDuration=form(value))
    recovery = workout(start=T0, activeDuration="0s")
    for record, expected in ((current, value if 0 <= value <= 3600 else None), (recovery, 0)):
        await store(sessions, "exercise", record)
        async with sessions() as session:
            before = await snapshot(session)
        result = await call(sessions, "sessions")
        Interpretation.model_validate(result)
        json.dumps(result, allow_nan=False)
        [row] = result["summaries"]
        assert row["reported_active_seconds"] == expected
        assert ("invalid_reported_active_duration" in row["reason_codes"]) == (expected is None)
        assert row["interval_seconds"] == 3600 and row["continuous_running_seconds"] is None
        assert row["start"] == T0.isoformat()
        assert row["end"] == (T0 + timedelta(hours=1)).isoformat()
        assert row["calories_kcal"] == 123 and row["distance_meters"] == 4567
        assert row["exercise_type"] == "RUNNING" and row["source"]
        assert row["evidence"][0]["record"] == original["name"]
        async with sessions() as session:
            assert await snapshot(session) == before


@pytest.mark.parametrize("fields", [*((f,) for f in OPTIONAL_METRICS), tuple(OPTIONAL_METRICS)])
@pytest.mark.parametrize("value", METRIC_VALUES)
@pytest.mark.parametrize("corrected", [False, True], ids=["new", "correction"])
async def test_session_optional_metric_sign_and_recovery(sessions, fields, value, corrected):
    original = workout(start=T0)
    neighbor = workout("neighbor", start=T0)
    neighbor["dataSource"] = PHONE
    if corrected:
        await store(sessions, "exercise", original)
    previous = None
    for selected in (value, "-0", "2345.5"):
        metrics = {**original["exercise"]["metricsSummary"], **dict.fromkeys(fields, selected)}
        current = workout(start=T0, metricsSummary=metrics)
        await store(sessions, "exercise", current, neighbor)
        async with sessions() as session:
            before = await snapshot(session)
        result = await call(sessions, "sessions")
        Interpretation.model_validate(result)
        json.dumps(result, allow_nan=False)
        assert result["total_summaries"] == 2
        row = next(r for r in result["summaries"] if r["evidence"][0]["record"] == original["name"])
        negative = [field for field in OPTIONAL_METRICS if Decimal(str(metrics[field])) < 0]
        reasons = [f"negative_measurement:{field}" for field in negative]
        assert row["evidence"][0]["reason_codes"] == reasons
        assert all(reason in row["reason_codes"] for reason in reasons)
        for (field, column), divisor in zip(OPTIONAL_METRICS.items(), (1, 1000), strict=True):
            assert row[column] == (None if field in negative else float(metrics[field]) / divisor)
        assert row["reported_active_seconds"] == 3500 and row["interval_seconds"] == 3600
        assert row["start"] == T0.isoformat()
        assert row["end"] == (T0 + timedelta(hours=1)).isoformat()
        assert row["source"] and row["exercise_type"] == "RUNNING"
        assert row["overlaps"] == [neighbor["name"]]
        if previous is not None:
            assert row == {
                **previous,
                **{column: row[column] for column in OPTIONAL_METRICS.values()},
                "reason_codes": row["reason_codes"],
                "evidence": [{**previous["evidence"][0], "reason_codes": reasons}],
            }
        async with sessions() as session:
            assert await snapshot(session) == before
        previous = row


@pytest.mark.parametrize("fields", [*((f,) for f in OPTIONAL_METRICS), tuple(OPTIONAL_METRICS)])
async def test_negative_session_metrics_do_not_revive_preserved_columns(sessions, fields):
    original = workout(start=T0)
    await store(sessions, "exercise", original)
    metrics = {**original["exercise"]["metricsSummary"], **dict.fromkeys(fields, "-1e-999999")}
    current = workout(start=T0, activeDuration="NaN", metricsSummary=metrics)
    written = await store(sessions, "exercise", current)
    assert written.normalization_skipped == 1
    async with sessions() as session:
        retained = await session.scalar(select(ExerciseSession))
        assert (retained.calories_kcal, retained.distance_meters) == (123, 4567)
        assert (await session.scalar(select(RawHealthData))).raw_json == current
        assert (await session.scalar(select(RawHealthDataVersion))).raw_json == original
        before = await snapshot(session)
    [row] = (await call(sessions, "sessions"))["summaries"]
    for field in fields:
        assert row[OPTIONAL_METRICS[field]] is None
        assert f"negative_measurement:{field}" in row["evidence"][0]["reason_codes"]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "kind,field,model,column,factor",
    [
        ("distance", "millimeters", Distance, "meters", 0.001),
        ("active-energy-burned", "kcal", EnergyExpenditure, "kcal", 1),
        ("active-energy-burned", "kcalSum", EnergyExpenditure, "kcal", 1),
        ("total-calories", "kcal", EnergyExpenditure, "kcal", 1),
        ("total-calories", "kcalSum", EnergyExpenditure, "kcal", 1),
        ("weight", "weightGrams", Weight, "kilograms", 0.001),
        ("height", "heightMillimeters", ScalarMetric, "value", 0.1),
        ("body-fat", "percentage", ScalarMetric, "value", 1),
        ("exercise", "distanceMillimeters", ExerciseSession, "distance_meters", None),
        *[
            (kind, field, health.RespiratoryRate, "breaths_per_minute", 1)
            for kind in ("daily-respiratory-rate", "respiratory-rate-sleep-summary")
            for field in ("breathsPerMinute", "fullSleepStats.breathsPerMinute")
        ],
        ("vo2-max", "vo2Max", health.Vo2Max, "ml_per_kg_min", 1),
        ("daily-vo2-max", "vo2Max", health.Vo2Max, "ml_per_kg_min", 1),
        ("run-vo2-max", "runVo2Max", health.Vo2Max, "ml_per_kg_min", 1),
        ("core-body-temperature", "temperatureCelsius", health.Temperature, "celsius", 1),
        *[
            ("daily-sleep-temperature-derivations", field, health.Temperature, column, 1)
            for field, column in (
                ("temperatureCelsius", "celsius"),
                ("nightlyTemperatureCelsius", "celsius"),
                ("baselineTemperatureCelsius", "baseline_celsius"),
            )
        ],
    ],
)
@pytest.mark.parametrize("corrected", [False, True], ids=["new", "same-id-correction"])
@pytest.mark.parametrize(
    "value,reason",
    [
        *[(v, None) for v in (1234, 1234.56789, "1234.56789", "5e-321", 0, "0", "-0", "0e99999")],
        *[(v, None) for v in NEGATIVE_GRAMS[:4]],
        *((v, NUMERIC) for v in ("-1e-999999", "-1e999999")),
        ("1e309", None),
        (10**309, None),
        (1.7e308, None),
        *[(v, NUMERIC) for v in (5e-324, "5e-324", "-5e-324", "1e-999999", "1e999999")],
        *[(v, MISSING) for v in ("NaN", "sNaN", "Infinity", "-Infinity")],
        *[(f"1e{sign}99999999999999999999", NUMERIC) for sign in ("", "-")],
        *[(v, MISSING) for v in (None, True, "bad", {}, [], ...)],
    ],
)
async def test_reader_scales_current_raw_values_without_mutating_archive(
    sessions, kind, field, model, column, factor, corrected, value, reason
):
    action = (
        "physiology" if kind in PHYSIOLOGY else "sessions" if kind == "exercise" else "longitudinal"
    )
    signed = kind in ("core-body-temperature", "daily-sleep-temperature-derivations")
    if not signed and value in [*NEGATIVE_GRAMS, "-1e999999"]:
        reason = f"negative_measurement:{field}"
    if factor == 1 and value in ("1e309", 10**309):
        reason = NUMERIC
    if factor == 1 and (value in (5e-324, "5e-324") or (signed and value == "-5e-324")):
        reason = None
    if value is ... and kind != "exercise":
        reason = "unsupported_payload_shape"
    if kind == "exercise":
        seed = exercise("scaled", exerciseType="WALKING")
        seed[REGISTRY[kind].json_key]["metricsSummary"][field] = 72000
    else:
        seed = point(kind, "scaled", {"sampleTime": {"physicalTime": T0.isoformat()}})
        seed[REGISTRY[kind].json_key].update(
            interval={"startTime": T0.isoformat(), "endTime": T0.replace(hour=1).isoformat()},
            **{field.split(".")[0]: {field.split(".")[1]: 72000} if "." in field else 72000},
        )
    seed[REGISTRY[kind].json_key].update(
        {"date": str(T0.date())} if kind.startswith("daily-") else {}
    )
    neighbor = deepcopy(seed)
    neighbor.update(name=seed["name"] + "-neighbor", dataSource=PHONE)
    if "vo2" in kind:
        neighbor["dataSource"] = seed["dataSource"]
        neighbor[REGISTRY[kind].json_key]["measurementMethod"] = "other"
    current = deepcopy(seed)
    payload = current[REGISTRY[kind].json_key]
    target = payload["metricsSummary"] if kind == "exercise" else payload
    target = target[field.split(".")[0]] if "." in field else target
    if value is ...:
        target.pop(field.split(".")[-1])
    else:
        target[field.split(".")[-1]] = value
    arguments = {} if action == "sessions" else {"data_type": kind}
    if corrected:
        await store(sessions, kind, seed, neighbor)
        cursor = (await call(sessions, action, limit=1, **arguments))["next_cursor"]
    written = await store(sessions, kind, current, neighbor)
    async with sessions() as session:
        before = await snapshot(session)
        versions = (await session.scalars(select(RawHealthDataVersion))).all()
        assert len(versions) == int(corrected)
        if corrected:
            assert versions[0].version == 1 and versions[0].raw_json == seed
            # Out-of-range corrections preserve the old normalized row; the reader
            # must still use the current raw value, including its missing result.
            if written.normalization_skipped and not signed:
                old = await session.scalar(
                    select(model).where(model.google_data_point_id == seed["name"])
                )
                assert old is not None and float(getattr(old, column)) == (
                    72000 if factor in (0.1, 1) else 72
                )
        if corrected:
            assert (await call(sessions, action, cursor=cursor, **arguments))[
                "code"
            ] == "stale_cursor"
    result = await call(sessions, action, **arguments)
    Interpretation.model_validate(result)
    json.dumps(result, allow_nan=False)
    assert result["total_summaries"] == 2
    row = next(r for r in result["summaries"] if r["evidence"][0]["record"] == seed["name"])
    evidence = row["evidence"][0]
    assert evidence["reason_codes"] == ([reason] if reason else [])
    assert (reason in row["reason_codes"]) if reason else NUMERIC not in row["reason_codes"]
    actual = row["distance_meters"] if action == "sessions" else evidence["value"]
    if reason:
        assert actual is None
    elif value in ("1e309", 10**309):
        assert actual == pytest.approx(1e308 if kind == "height" else 1e306)
    else:
        # Keep the historical multiply (measurements) versus divide (sessions)
        # rounding order, rather than merely checking an approximate normal value.
        expected = float(value) / 1000 if factor is None else float(value) * factor
        assert actual == expected
    if action == "sessions":
        assert row["calories_kcal"] == 300 and row["reported_active_seconds"] == 1800
        assert row["exercise_type"] == "WALKING" and row["interval_seconds"] == 5400
        assert row["overlaps"] == [neighbor["name"]]
    else:
        assert row["statistics"]["count"] == int(reason is None)
        assert row["statistics"]["median"] == actual
        assert row["state"] == ("unsupported" if reason else "available")
        assert row["change"] is None
        assert row["age_days"] == (None if reason else 0)
        assert row["last_date"] == (None if reason else str(T0.date()))
        assert row["freshness"] == ("absent" if reason else "recent")
        assert evidence["record"] == seed["name"]
        assert evidence["field"] == (None if value is ... else field)
        assert evidence["start"] == T0.isoformat()
        assert evidence["end"] == T0.replace(hour=1).isoformat()
        assert row["first_date"] == row["last_date"]
    filtered = await call(
        sessions, action, source=row["source"], definition=row["definition"], **arguments
    )
    assert filtered["summaries"] == [row]
    page = await call(sessions, action, limit=1, **arguments)
    rest = await call(sessions, action, cursor=page["next_cursor"], **arguments)
    assert page["summaries"] + rest["summaries"] == result["summaries"]
    async with sessions() as session:
        assert await snapshot(session) == before

    if not signed and kind != "exercise" and value in [*NEGATIVE_GRAMS, "-1e999999"]:
        await store(sessions, kind, seed)
        assert (await call(sessions, action, cursor=page["next_cursor"], **arguments))[
            "code"
        ] == "stale_cursor"
        async with sessions() as session:
            versions = (await session.scalars(select(RawHealthDataVersion))).all()
            assert [v.raw_json for v in sorted(versions, key=lambda v: v.version)] == (
                [seed, current] if corrected else [current]
            )
            before = await snapshot(session)
        recovered = await call(sessions, action, **arguments)
        restored = next(
            r for r in recovered["summaries"] if r["evidence"][0]["record"] == seed["name"]
        )
        assert restored["statistics"]["count"] == 1
        assert restored["evidence"][0]["value"] == 72000 * factor
        assert restored["evidence"][0]["reason_codes"] == []
        async with sessions() as session:
            assert await snapshot(session) == before


@pytest.mark.parametrize(
    "value,reason",
    [
        *[(v, MISSING) for v in ("NaN", "sNaN", "Infinity", "-Infinity", "bad", None)],
        *[(f"1e{sign}99999999999999999999", NUMERIC) for sign in ("", "-")],
        *[(v, NUMERIC) for v in ("1e999999", "-1e999999", "1e-999999")],
    ],
)
async def test_unscaled_and_scaled_fields_share_numeric_reasons(sessions, value, reason):
    # body-fat percentage keeps its unit; weight grams scale to kilograms.
    for kind, field in (("body-fat", "percentage"), ("weight", "weightGrams")):
        record = point(kind, kind, {"sampleTime": {"physicalTime": T0.isoformat()}, field: value})
        await store(sessions, kind, record)
    for kind, field in (("body-fat", "percentage"), ("weight", "weightGrams")):
        expected = f"negative_measurement:{field}" if value == "-1e999999" else reason
        row = (await call(sessions, "longitudinal", data_type=kind))["summaries"][0]
        assert row["evidence"][0]["reason_codes"] == [expected], kind
        assert row["evidence"][0]["value"] is None and expected in row["reason_codes"]


@pytest.mark.parametrize(
    "kind,metric,field,raw_value,value",
    [*CASES[1:], ("distance", "distance", "millimeters", 1000, 1)]
    + [
        (kind, kind, field, 100, 100)
        for kind in ("active-energy-burned", "total-calories")
        for field in ("kcal", "kcalSum")
    ]
    + [
        (kind, kind, field, 100, 100)
        for kind, (_, fields) in PHYSIOLOGY.items()
        if "temperature" not in kind
        for field in fields
    ],
)
@pytest.mark.parametrize("negative", [*NEGATIVE_GRAMS, "-1e999999"])
async def test_negative_latest_measurement_does_not_advance_longitudinal_freshness(
    sessions, kind, metric, field, raw_value, value, negative
):
    for offset, supplied in ((-10, raw_value), (-9, raw_value * 2), (0, negative), (-9, negative)):
        record = measurement(kind, field, str(offset), supplied, T0 + timedelta(days=offset))
        record["name"] += str(supplied)
        if "." in field:
            record[REGISTRY[kind].json_key] = {"fullSleepStats": {"breathsPerMinute": supplied}}
        if kind.startswith("daily-"):
            record[REGISTRY[kind].json_key]["date"] = str((T0 + timedelta(days=offset)).date())
        at = T0 + timedelta(days=offset)
        record[REGISTRY[kind].json_key]["interval"] = {
            "startTime": at.isoformat(),
            "endTime": (at + timedelta(hours=1)).isoformat(),
        }
        await store(sessions, kind, record)
    async with sessions() as session:
        before = await snapshot(session)
    result = await call(
        sessions,
        "physiology" if kind in PHYSIOLOGY else "longitudinal",
        data_type=kind,
        start_date=str((T0 - timedelta(days=10)).date()),
    )
    json.dumps(result, allow_nan=False)
    [row] = result["summaries"]
    assert row["statistics"]["count"] == 2 and row["statistics"]["median"] == value * 1.5
    assert row["change"] == (
        None if kind in ("distance", "active-energy-burned", "total-calories") else value
    )
    assert row["age_days"] == 9 and row["freshness"] == ("recent" if "vo2" in kind else "stale")
    assert row["first_date"] == str((T0 - timedelta(days=10)).date())
    assert row["last_date"] == str((T0 - timedelta(days=9)).date())
    assert "conflicting_values_at_same_time" not in row["reason_codes"]
    assert row["evidence"][-1]["value"] is None
    assert row["evidence"][-1]["reason_codes"] == [f"negative_measurement:{field}"]
    async with sessions() as session:
        assert await snapshot(session) == before
