"""Daily corrections through the real page writer, normalizer and current readers."""

from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

import garmin_records
import pytest
from sqlalchemy import select, update
from test_garmin_normalize import DAY, NOW, garmin_sleep

from app.models.garmin import (
    GarminApiResponse,
    GarminDailyProjection,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import (
    Distance,
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
)
from app.services.providers import garmin_readers
from app.services.providers.garmin_normalize import normalize_window
from app.services.providers.garmin_versions import current_row

# Only fields needed for the projection; no implicit alternate raw input.
CASES = (
    ("steps", Steps, {"totalSteps": 7000}, ("totalSteps",), "steps"),
    ("steps", Distance, {"totalDistance": 5400}, ("totalDistance",), None),
    (
        "heart-rate",
        RestingHeartRate,
        {"restingHeartRate": 50},
        ("restingHeartRate",),
        "resting_bpm",
    ),
    ("sleep", SleepSession, garmin_sleep(DAY), ("sleepEndTimestampGMT",), "sleep_seconds"),
    ("hrv-summaries", HeartRateVariability, {"lastNightAvg": 40}, ("lastNightAvg",), "hrv_ms"),
    ("spo2", OxygenSaturation, {"averageSpO2": 97}, ("averageSpO2",), "spo2"),
    (
        "stress",
        ScalarMetric,
        {"values": {"overallStressLevel": 20}},
        ("values", "overallStressLevel"),
        None,
    ),
    (
        "daily-summaries",
        ScalarMetric,
        {"bodyBattery": {"latestValue": 40}},
        ("bodyBattery", "latestValue"),
        None,
    ),
)


def corrected(body, path, replacement):
    result = deepcopy(body)
    target = result
    for name in path[:-1]:
        target = target[name]
    if replacement == "missing":
        target.pop(path[-1])
    else:
        target[path[-1]] = {
            "null": None,
            "invalid": True,
            "overflow": 10**30,
            "negative": -1,
            "tiny-negative": -0.0001,
            "underflow": "-1e-10000",
        }[replacement]
    return result


@pytest.mark.parametrize("kind,model,body,path,metric", CASES)
@pytest.mark.parametrize("replacement", ["null", "missing", "invalid"])
async def test_daily_invalid_correction_hides_old_value_and_return_restores_it(
    sessions, kind, model, body, path, metric, replacement
):
    a = {**body, "calendarDate": DAY.isoformat()}
    b = corrected(a, path, replacement)
    evidence = (GarminApiResponse, GarminRawRecord, GarminRecordObservation, GarminDailyProjection)
    previous = {table: [] for table in evidence}
    stored_before = None
    for index, payload in enumerate((a, b, b, a)):
        await garmin_records.store_page(sessions, kind, [payload], NOW + timedelta(minutes=index))
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            visible = (await session.scalars(select(model).where(current_row(model)))).all()
            assert bool(visible) == (index in (0, 3))
            if metric:
                days = await garmin_readers.daily_values(session, DAY, DAY, ZoneInfo("UTC"))
                assert (metric in days.get(DAY, {})) == (index in (0, 3))
            # Invalid corrections retain the old normalized evidence and quarantine state.
            stored = (await session.scalars(select(model))).all()
            assert len(stored) == 1
            assert not stored[0].is_quarantined and stored[0].quarantine_reason is None
            saved = (await session.execute(select(model.__table__))).all()
            if stored_before is None:
                stored_before = saved
            assert saved == stored_before
            for table in evidence:
                # Compare actual columns rather than ORM instance identities across sessions.
                saved = (await session.execute(select(table.__table__).order_by(table.id))).all()
                assert saved[: len(previous[table])] == previous[table]
                if index == 2 and table is GarminDailyProjection:
                    assert saved == previous[table]
                previous[table] = saved
            states = (
                await session.scalars(
                    select(GarminDailyProjection)
                    .where(GarminDailyProjection.source_record_id == stored[0].source_record_id)
                    .order_by(GarminDailyProjection.id)
                )
            ).all()
            assert len(states) == (1, 2, 2, 3)[index]
            if index == 3:
                assert states[-1].raw_record_ids == states[0].raw_record_ids
                assert states[1].raw_record_ids != states[0].raw_record_ids
            assert (states[-1].normalized_key is not None) == (index in (0, 3))
            if index == 2:
                assert stats.inserted == stats.updated == 0
    assert len(previous[GarminRawRecord]) == 2
    assert len(previous[GarminRecordObservation]) == 4


@pytest.mark.parametrize(
    "kind,model,body,path,metric",
    [c for c in CASES if c[1] not in (SleepSession, HeartRateVariability, ScalarMetric)],
)
async def test_numeric_preparation_can_invalidate_a_daily_projection(
    sessions, kind, model, body, path, metric
):
    a = {**body, "calendarDate": str(DAY)}
    await garmin_records.store_page(sessions, kind, [a], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await garmin_records.store_page(
        sessions, kind, [corrected(a, path, "overflow")], NOW + timedelta(minutes=1)
    )
    async with sessions.begin() as session:
        stats = await normalize_window(session, DAY, DAY)
        assert stats.skipped == 1
    async with sessions() as session:
        assert (await session.scalars(select(model).where(current_row(model)))).all() == []
        assert len((await session.scalars(select(model))).all()) == 1


@pytest.mark.parametrize(
    "kind,model,body,path,metric,zero",
    [c + (0,) for c in CASES if c[1] is not SleepSession]
    + [
        c + (zero,)
        for c in CASES
        if c[-1] in ("resting_bpm", "hrv_ms", "spo2")
        for zero in (-0.0, "-0e-10000")
    ],
)
async def test_daily_zero_is_valid(sessions, kind, model, body, path, metric, zero):
    payload = deepcopy(body)
    target = payload
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = zero
    payload["calendarDate"] = str(DAY)
    await garmin_records.store_page(sessions, kind, [payload], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        assert len((await session.scalars(select(model).where(current_row(model)))).all()) == 1
        if metric:
            assert (await garmin_readers.daily_values(session, DAY, DAY, ZoneInfo("UTC")))[DAY][
                metric
            ] == 0


@pytest.mark.parametrize("kind,model,body,path,metric", CASES)
async def test_invalidation_and_restoration_preserve_user_quarantine(
    sessions, kind, model, body, path, metric
):
    a = {**body, "calendarDate": str(DAY)}
    await garmin_records.store_page(sessions, kind, [a], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
        await session.execute(
            update(model).values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
    for index, payload in enumerate((corrected(a, path, "null"), a), start=1):
        await garmin_records.store_page(sessions, kind, [payload], NOW + timedelta(minutes=index))
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            row = (await session.scalars(select(model))).one()
            assert row.is_quarantined and row.quarantine_reason == "Synthetic exclusion"
            if metric:
                assert metric not in (
                    await garmin_readers.daily_values(session, DAY, DAY, ZoneInfo("UTC"))
                ).get(DAY, {})


@pytest.mark.parametrize("end", ["equal", "reversed", "unrepresentable"])
async def test_invalidated_sleep_interval_keeps_old_session_but_hides_it(sessions, end):
    a = garmin_sleep(DAY)
    b = {
        **a,
        "sleepEndTimestampGMT": {
            "equal": a["sleepStartTimestampGMT"],
            "reversed": a["sleepStartTimestampGMT"] - 1,
            "unrepresentable": 10**30,
        }[end],
    }
    for index, payload in enumerate((a, b)):
        await garmin_records.store_page(
            sessions, "sleep", [payload], NOW + timedelta(minutes=index)
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            assert len((await session.scalars(select(SleepSession))).all()) == 1
            days = await garmin_readers.daily_values(session, DAY, DAY, ZoneInfo("UTC"))
            assert ("sleep_seconds" in days.get(DAY, {})) == (index == 0)


@pytest.mark.parametrize("kind,model,body,path,metric", CASES)
async def test_daily_validity_leaves_other_providers_eligible(
    sessions, kind, model, body, path, metric
):
    from app.services.providers.garmin_normalize import build_rows

    a = {**body, "calendarDate": str(DAY)}
    prepared, _ = build_rows({kind: {str(DAY): a}}, DAY, DAY, "Synthetic")
    async with sessions.begin() as session:
        for provider in ("GOOGLE", "DEMO"):
            for row in prepared[model]:
                session.add(
                    model(
                        **{
                            **row,
                            "provider": provider,
                            "google_data_point_id": provider + row["google_data_point_id"],
                        }
                    )
                )
    for index, payload in enumerate((a, corrected(a, path, "null"))):
        await garmin_records.store_page(sessions, kind, [payload], NOW + timedelta(minutes=index))
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            rows = (await session.scalars(select(model).where(current_row(model)))).all()
            assert sorted(row.provider for row in rows) == (
                ["DEMO", "GARMIN", "GOOGLE"] if index == 0 else ["DEMO", "GOOGLE"]
            )
