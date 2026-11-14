"""Daily scalar source signs and publication boundaries through the real archive."""

import json
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_records import store_page
from sqlalchemy import event, select, update
from test_garmin_daily_projections import CASES
from test_garmin_normalize import DAY, NOW
from test_garmin_step_validity import insert_raw_json, only_select

from app.models.garmin import (
    GarminApiResponse,
    GarminDailyProjection,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import HeartRateVariability, OxygenSaturation, RestingHeartRate
from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows
from app.services.providers.garmin_readers import archive_bounds, daily_values

ZONE = ZoneInfo("UTC")
SCALARS = [c for c in CASES if c[-1] in ("resting_bpm", "hrv_ms", "spo2")]
TABLES = (
    GarminApiResponse,
    GarminRawRecord,
    GarminRecordObservation,
    GarminDailyProjection,
    RestingHeartRate,
    HeartRateVariability,
    OxygenSaturation,
)


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("normalized", [0, 50])
@pytest.mark.parametrize("token", ["-1e-1000", '"-1e-1000"', "1e-1000", "0", "-0e-1000"])
@pytest.mark.parametrize(
    "kind,model,body,path,metric",
    SCALARS
    + [
        (
            "daily-summaries",
            RestingHeartRate,
            {"heartRate": {"restingValue": 50}},
            ("heartRate", "restingValue"),
            "resting_bpm",
        ),
        (
            "hrv",
            HeartRateVariability,
            {"hrvSummary": {"lastNightAvg": 50}},
            ("hrvSummary", "lastNightAvg"),
            "hrv_ms",
        ),
    ],
)
async def test_json_tokens_keep_scalar_sign_and_field_selection(
    sessions, projected, normalized, token, kind, model, body, path, metric
):
    positive = payload(body, path, normalized)
    # A valid alternate must not rescue a negative primary member or HRV record.
    alternate = {
        "daily-summaries": ("heart-rate", {"restingHeartRate": 60}),
        "hrv-summaries": ("hrv", {"hrvSummary": {"lastNightAvg": 60}}),
    }.get(kind)
    if alternate:
        await store_page(sessions, alternate[0], [{"calendarDate": str(DAY), **alternate[1]}], NOW)
    rows, _ = build_rows({kind: {str(DAY): positive}}, DAY, DAY, "Test")
    values = rows[model][0]
    body_text = json.dumps(payload(body, path, "TOKEN")).replace('"TOKEN"', token)
    async with sessions.begin() as session:
        await insert_raw_json(session, kind, json.dumps(payload(body, path, 61)))
        raw_id = await insert_raw_json(session, kind, body_text)
        await write_rows(session, model, [values])
        if projected:
            session.add(
                GarminDailyProjection(
                    source_record_id=values["source_record_id"],
                    raw_record_ids={kind: raw_id},
                    normalized_key=values["google_data_point_id"],
                )
            )
    expected = None if "-1e" in token else normalized
    await assert_read(sessions, metric, expected)
    if projected:
        await store_page(sessions, kind, [payload(body, path, 80)], NOW + timedelta(minutes=1))
        await assert_read(sessions, metric, expected)


def payload(body, path, value, day=DAY):
    result = deepcopy(body)
    target = result
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    return {**result, "calendarDate": str(day)}


async def snapshot(session):
    return {
        model: (await session.execute(select(model.__table__).order_by(model.id))).all()
        for model in TABLES
    }


async def assert_read(sessions, metric, expected):
    engine = sessions.kw["bind"].sync_engine
    async with sessions() as session:
        before = await snapshot(session)
        event.listen(engine, "before_cursor_execute", only_select)
        try:
            for _ in range(2):
                assert await daily_values(session, DAY, DAY, ZONE, metrics=(metric,)) == (
                    {} if expected is None else {DAY: {metric: expected}}
                )
                if metric == "resting_bpm":
                    bounds = await archive_bounds(session, "heart", DAY, ZONE)
                    assert bounds.earliest == bounds.latest == (None if expected is None else DAY)
        finally:
            event.remove(engine, "before_cursor_execute", only_select)
        assert await snapshot(session) == before


@pytest.mark.parametrize("kind,model,body,path,metric", SCALARS)
@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.25, None),
        (-0.0001, None),
        (-1e-300, None),
        ("-1e-10000", None),
        (0, 0),
        (-0.0, 0),
        ("-0e-10000", 0),
        (12.25, 12.25),
        ("12.25", 12.25),
        ("1e-10000", 0),
    ],
)
async def test_committed_scalars_ignore_pending_inputs_and_preserve_every_version(
    sessions, kind, model, body, path, metric, value, expected
):
    previous = None
    positive = body[path[-1]]
    for index, source in enumerate((positive, value, value, positive)):
        await store_page(
            sessions, kind, [payload(body, path, source)], NOW + timedelta(minutes=index)
        )
        await assert_read(sessions, metric, previous)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        previous = expected if index in (1, 2) else positive
        await assert_read(sessions, metric, previous)
        async with sessions() as session:
            before = await snapshot(session)
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
            assert stats.inserted == stats.updated == 0
            assert await snapshot(session) == before
    async with sessions() as session:
        saved = await snapshot(session)
        assert len(saved[GarminRawRecord]) == 2
        assert len(saved[GarminRecordObservation]) == 4
        assert len(saved[GarminDailyProjection]) == 3


@pytest.mark.parametrize("kind,model,body,path,metric", SCALARS)
@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("negative", [-1, -0.0001, -1e-300, "-1e-10000", -(10**400)])
async def test_legacy_scalars_use_current_raw_sign_without_rewriting_normalized_rows(
    sessions, kind, model, body, path, metric, version, observed, negative
):
    positive = body[path[-1]]
    a = payload(body, path, positive)
    rows, _ = build_rows({kind: {str(DAY): a}}, DAY, DAY, "Synthetic")
    async with sessions.begin() as session:
        for selected in ("v1", "v2") if version == "mixed" else (version,):
            values = dict(rows[model][0])
            if selected == "v1":
                values["google_data_point_id"] = values["google_data_point_id"].removesuffix(":v2")
            await write_rows(session, model, [values])
    await assert_read(sessions, metric, positive)  # Normalized-only legacy archive.
    for index, value in enumerate((positive, negative, positive, negative)):
        if observed:
            await store_page(
                sessions, kind, [payload(body, path, value)], NOW + timedelta(minutes=index)
            )
        elif index < 2:
            async with sessions.begin() as session:
                session.add(
                    GarminRawRecord(
                        kind=kind,
                        key=str(DAY),
                        body=payload(body, path, value),
                        body_sha256=str(index) * 64,
                        first_seen_at=NOW + timedelta(minutes=index),
                    )
                )
        expected = None if index % 2 or (not observed and index > 1) else positive
        await assert_read(sessions, metric, expected)
    async with sessions() as session:
        assert (await session.scalars(select(GarminDailyProjection))).all() == []


@pytest.mark.parametrize("kind,model,body,path,metric", SCALARS)
async def test_negative_corrections_and_restoration_preserve_user_quarantine(
    sessions, kind, model, body, path, metric
):
    for index, value in enumerate((body[path[-1]], "-1e-10000", body[path[-1]])):
        await store_page(
            sessions, kind, [payload(body, path, value)], NOW + timedelta(minutes=index)
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
            if index == 0:
                await session.execute(
                    update(model).values(
                        is_quarantined=True, quarantine_reason="Synthetic exclusion"
                    )
                )
        await assert_read(sessions, metric, None)
        async with sessions() as session:
            row = (await session.scalars(select(model))).one()
            assert row.is_quarantined and row.quarantine_reason == "Synthetic exclusion"


@pytest.mark.parametrize("garmin_first", [False, True])
@pytest.mark.parametrize("kind,model,body,path,metric", SCALARS)
async def test_negative_scalar_never_enters_source_resolution(
    sessions, garmin_first, kind, model, body, path, metric
):
    from derived_records import store, summary

    from app.services.dashboard_views.dashboard import dashboard_data
    from app.services.identities.source_precedence import Policy
    from app.services.readers.dashboard_queries import DashboardQuery

    google_kind, field = {
        "resting_bpm": ("daily-resting-heart-rate", "beatsPerMinute"),
        "hrv_ms": ("daily-heart-rate-variability", "rmssd"),
        "spo2": ("daily-oxygen-saturation", "averagePercentage"),
    }[metric]
    await store(sessions, google_kind, summary(google_kind, day=DAY, **{field: 80}))
    policy = Policy(health=("GARMIN", "FITBIT") if garmin_first else ("FITBIT", "GARMIN"))
    for index, value in enumerate((body[path[-1]], "-1e-10000", body[path[-1]])):
        await store_page(
            sessions, kind, [payload(body, path, value)], NOW + timedelta(minutes=index)
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            before = await snapshot(session)
            data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"), policy=policy)
            day = data["days"][0]
            if index == 1:
                assert day[metric] == 80
                assert day["sources"][metric] == {
                    "source": "FITBIT",
                    "label": "Fitbit",
                    "alternatives": [],
                }
            else:
                assert day[metric] == (body[path[-1]] if garmin_first else 80)
                assert len(day["sources"][metric]["alternatives"]) == 1
            assert await snapshot(session) == before


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize(
    "primary,expected",
    [
        (-1, None),
        (-0.0001, None),
        ("-1e-10000", None),
        (0, 0),
        ("-0e-10000", 0),
        (12.25, 12.25),
        (None, 60),
        (True, 60),
        ("broken", 60),
    ],
)
async def test_resting_field_selection_keeps_negative_primary_over_positive_alternate(
    sessions, projected, primary, expected
):
    records = {
        "daily-summaries": {"calendarDate": str(DAY), "heartRate": {"restingValue": primary}},
        "heart-rate": {"calendarDate": str(DAY), "restingHeartRate": 60},
    }
    for kind, body in records.items():
        await store_page(sessions, kind, [body], NOW)
    async with sessions.begin() as session:
        if projected:
            await normalize_window(session, DAY, DAY)
        else:
            rows, _ = build_rows(
                {k: {str(DAY): b} for k, b in records.items()}, DAY, DAY, "Synthetic"
            )
            await write_rows(session, RestingHeartRate, rows[RestingHeartRate])
    await assert_read(sessions, "resting_bpm", expected)


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize(
    "primary,expected",
    [
        ({"lastNightAvg": -1}, None),
        ({"lastNightAvg": "-1e-10000"}, None),
        ({"lastNightAvg": True}, None),
        ({"lastNightAvg": None}, None),
        ({"lastNightAvg": 0}, 0),
        ({"status": "synthetic"}, None),
        ({}, 40),
        (None, 40),
    ],
)
async def test_hrv_chooses_a_whole_record_before_testing_its_measurement(
    sessions, projected, primary, expected
):
    await store_page(
        sessions,
        "hrv",
        [
            {
                "calendarDate": str(DAY),
                "hrvSummary": {"lastNightAvg": 40},
            }
        ],
        NOW,
    )
    if primary:
        await store_page(sessions, "hrv-summaries", [{"calendarDate": str(DAY), **primary}], NOW)
    elif primary == {}:
        # Historical archives can contain an empty body with an independently known day key.
        async with sessions.begin() as session:
            session.add(
                GarminRawRecord(
                    kind="hrv-summaries",
                    key=str(DAY),
                    body={},
                    body_sha256="0" * 64,
                    first_seen_at=NOW,
                )
            )
    async with sessions.begin() as session:
        if projected:
            await normalize_window(session, DAY, DAY)
        else:
            rows, _ = build_rows(
                {
                    "hrv-summaries": {str(DAY): primary},
                    "hrv": {str(DAY): {"hrvSummary": {"lastNightAvg": 40}}},
                },
                DAY,
                DAY,
                "Synthetic",
            )
            await write_rows(session, HeartRateVariability, rows.get(HeartRateVariability, []))
    await assert_read(sessions, "hrv_ms", expected)


async def test_resting_bounds_only_use_valid_current_days(sessions):
    for offset, value in (
        (-5, -1),
        (-4, 0),
        (-3, 12),
        (-2, "-1e-10000"),
        (-1, 90),
        (0, 100),
        (1, 80),
    ):
        day = DAY + timedelta(days=offset)
        await store_page(
            sessions,
            "heart-rate",
            [
                {
                    "calendarDate": str(day),
                    "restingHeartRate": value,
                }
            ],
            NOW,
        )
        async with sessions.begin() as session:
            await normalize_window(session, day, day)
    async with sessions.begin() as session:
        await session.execute(
            update(RestingHeartRate)
            .where(
                RestingHeartRate.source_record_id.in_(
                    (
                        f"resting-heart-rate:{DAY - timedelta(days=1)}",
                        f"resting-heart-rate:{DAY}",
                    )
                )
            )
            .values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
        old_day = DAY - timedelta(days=5)
        rows, _ = build_rows(
            {
                "heart-rate": {
                    str(old_day): {
                        "restingHeartRate": 50,
                    }
                }
            },
            old_day,
            old_day,
            "Synthetic",
        )
        row = rows[RestingHeartRate][0]
        row["google_data_point_id"] = row["google_data_point_id"].removesuffix(":v2")
        await write_rows(session, RestingHeartRate, [row])
    async with sessions() as session:
        before = await snapshot(session)
        bounds = await archive_bounds(session, "heart", DAY, ZONE)
        assert bounds.earliest == DAY - timedelta(days=4)
        assert bounds.latest == DAY - timedelta(days=3)
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "metric,alternate,primary",
    [
        ("resting_bpm", "heart-rate", "daily-summaries"),
        ("hrv_ms", "hrv", "hrv-summaries"),
    ],
)
async def test_new_primary_cannot_replace_committed_alternate_before_normalization(
    sessions, metric, alternate, primary
):
    body = (
        {"restingHeartRate": 60}
        if metric == "resting_bpm"
        else {"hrvSummary": {"lastNightAvg": 60}}
    )
    await store_page(sessions, alternate, [{"calendarDate": str(DAY), **body}], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await assert_read(sessions, metric, 60)
    for index, value in enumerate(("-1e-10000", 50)):
        body = (
            {"heartRate": {"restingValue": value}}
            if metric == "resting_bpm"
            else {"lastNightAvg": value}
        )
        await store_page(
            sessions,
            primary,
            [{"calendarDate": str(DAY), **body}],
            NOW + timedelta(minutes=index + 1),
        )
        await assert_read(sessions, metric, 60 if index == 0 else None)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        await assert_read(sessions, metric, None if index == 0 else 50)


@pytest.mark.parametrize("kind,model,body,path,metric", SCALARS)
@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_completed_legacy_projection_owns_its_input_in_both_normalized_versions(
    sessions, kind, model, body, path, metric, version
):
    # Reconstruct preexisting projection evidence from the real archived input and
    # converter output, retaining its historical normalized key.
    await store_page(sessions, kind, [payload(body, path, -0.0001)], NOW)
    rows, _ = build_rows({kind: {str(DAY): payload(body, path, -0.0001)}}, DAY, DAY, "Synthetic")
    values = rows[model][0]
    if version == "v1":
        values["google_data_point_id"] = values["google_data_point_id"].removesuffix(":v2")
    async with sessions.begin() as session:
        await write_rows(session, model, [values])
        raw_id = await session.scalar(select(GarminRawRecord.id))
        session.add(
            GarminDailyProjection(
                source_record_id=values["source_record_id"],
                raw_record_ids={kind: raw_id},
                normalized_key=values["google_data_point_id"],
            )
        )
    await assert_read(sessions, metric, None)
    await store_page(sessions, kind, [payload(body, path, 50)], NOW + timedelta(minutes=1))
    await assert_read(sessions, metric, None)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await assert_read(sessions, metric, 50)


@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.25, None),
        (-0.0001, None),
        (-1e-300, None),
        ("-1e-10000", None),
        (0, 0),
        (-0.0, 0),
        ("-0e-10000", 0),
        (12.25, 12.25),
    ],
)
async def test_nested_hrv_alternate_retains_the_source_sign(sessions, value, expected):
    await store_page(
        sessions,
        "hrv",
        [
            {
                "calendarDate": str(DAY),
                "hrvSummary": {"lastNightAvg": value},
            }
        ],
        NOW,
    )
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    await assert_read(sessions, "hrv_ms", expected)
