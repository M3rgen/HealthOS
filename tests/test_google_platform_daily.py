"""Synthetic platform conflicts through production ingestion, dashboard and history reads."""

from datetime import UTC, date, datetime, timedelta

import pytest
from derived_records import BAND, DAY, PHONE, T0, sleep, snapshot, steps, store, summary
from garmin_records import daily_steps, store_page
from sqlalchemy import select, update

from app.core.config import get_settings
from app.models.health import (
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    SleepSession,
    Steps,
)
from app.models.identity_tables import RawHealthDataSupersession
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_history import history_data
from app.services.providers.garmin_normalize import normalize_window
from app.services.providers.ingestion_readers import ARCHIVES
from app.services.readers.dashboard_queries import DashboardQuery

METRICS = (
    ("steps", "steps", None, Steps),
    ("sleep", "sleep_seconds", None, SleepSession),
    ("daily-resting-heart-rate", "resting_bpm", "beatsPerMinute", RestingHeartRate),
    ("daily-heart-rate-variability", "hrv_ms", "rmssd", HeartRateVariability),
    ("daily-oxygen-saturation", "spo2", "averagePercentage", OxygenSaturation),
)
HISTORY = (("steps", "steps"), ("sleep", "sleep_seconds"), ("heart", "resting_bpm"))
DST_DAYS = (
    ("Europe/Brussels", date(2026, 3, 29), 23, {2: None}),
    ("Europe/Brussels", date(2026, 10, 25), 25, {2: 2}),
    ("Pacific/Chatham", date(2026, 9, 27), 23, {2: 0.75, 3: 0.25}),
    ("Pacific/Chatham", date(2026, 4, 5), 25, {2: 1.25, 3: 1.75}),
    ("Australia/Lord_Howe", date(2026, 10, 4), 23.5, {2: 0.5}),
    ("Australia/Lord_Howe", date(2026, 4, 5), 24.5, {1: 1.5}),
)


async def project(sessions, monkeypatch, preferred="FITBIT", day=DAY, zone="UTC", **options):
    order = (preferred, *(code for code in ("FITBIT", "GARMIN", "HEALTH_KIT") if code != preferred))
    monkeypatch.setattr(get_settings(), "source_precedence_health", ",".join(order))
    async with sessions() as session:
        before = await snapshot(session)
        data = await dashboard_data(session, query=DashboardQuery(day, 2, zone), **options)
        if not options.get("include_source_deleted"):
            for metric, key in HISTORY:
                history = await history_data(session, metric, 1, zone, day)
                values = [row[key] for row in data["days"]]
                assert [row["value"] for row in history["days"][:2]] == values
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted
    return data["days"][0], data["charts"]["steps"]


def candidates(day, metric):
    selected = day["sources"][metric]
    return {selected["source"]: day[metric]} | {
        item["source"]: item["value"] for item in selected["alternatives"]
    }


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("garmin", [False, True])
@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT", "GARMIN"])
async def test_platform_steps_and_hours(sessions, monkeypatch, preferred, garmin, reverse):
    records = [
        steps("band", 100, T0, source=BAND),
        steps("zero", 0, T0 + timedelta(hours=1), source=BAND),
        steps("phone", 200, T0 + timedelta(hours=2), source=PHONE),
    ]
    await store(sessions, "steps", *(reversed(records) if reverse else records))
    if garmin:
        await store_page(sessions, "steps", [daily_steps(DAY, steps=300)], T0)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
    day, chart = await project(
        sessions,
        monkeypatch,
        preferred,
        archives=tuple(reversed(ARCHIVES)) if reverse else ARCHIVES,
    )
    winner = preferred if garmin or preferred != "GARMIN" else "FITBIT"
    assert day["sources"]["steps"]["source"] == winner
    assert candidates(day, "steps") == {"FITBIT": 100, "HEALTH_KIT": 200} | (
        {"GARMIN": 300} if garmin else {}
    )
    assert chart == (
        [100.0, 0.0] + [None] * 22
        if winner == "FITBIT"
        else [None, None, 200.0] + [None] * 21
        if winner == "HEALTH_KIT"
        else [None] * 24
    )


@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT"])
async def test_sleep_sessions_and_assumptions_stay_with_platform(sessions, monkeypatch, preferred):
    await store(
        sessions,
        "sleep",
        sleep("band-a", T0, T0 + timedelta(hours=4), summary={"minutesAsleep": 120}),
        sleep(
            "band-b",
            T0 + timedelta(hours=2),
            T0 + timedelta(hours=6),
            summary={"minutesAsleep": 120},
        ),
        sleep("phone", T0, T0 + timedelta(hours=8), source=PHONE),
    )
    day, _ = await project(sessions, monkeypatch, preferred)
    assert candidates(day, "sleep_seconds") == {"FITBIT": 10800, "HEALTH_KIT": 28800}
    assert day["sources"]["sleep_seconds"]["source"] == preferred
    assert day["sleep_assumptions"] == (
        ["overlap_estimated"] if preferred == "FITBIT" else ["awake_unknown"]
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT"])
async def test_daily_summaries_keep_values_with_labels_and_latest_within_platform(
    sessions, monkeypatch, clock, preferred, reverse
):
    sources = [(BAND, 60), (PHONE, 80)]
    for source, value in reversed(sources) if reverse else sources:
        for kind, _, field, _ in METRICS[2:]:
            await store(
                sessions,
                kind,
                summary(kind, source["platform"] + "-old", source=source, **{field: value - 10}),
            )
            clock.advance(seconds=1)
            await store(
                sessions, kind, summary(kind, source["platform"], source=source, **{field: value})
            )
            clock.advance(seconds=1)
    day, _ = await project(sessions, monkeypatch, preferred)
    for _, metric, _, _ in METRICS[2:]:
        assert candidates(day, metric) == {"FITBIT": 60, "HEALTH_KIT": 80}
        assert day["sources"][metric]["source"] == preferred


@pytest.mark.parametrize("kind,metric,field,model", METRICS)
@pytest.mark.parametrize(
    "source",
    [{}, {"platform": " "}, {"platform": None}, {"platform": 42}, {"platform": "future_device"}],
)
async def test_unknown_platform_keeps_its_source(
    sessions, monkeypatch, source, kind, metric, field, model
):
    await store(sessions, kind, measurement(kind, field, "other", source, 80))
    code = "FUTURE_DEVICE" if source.get("platform") == "future_device" else "GOOGLE"
    value = 4800 if kind == "sleep" else 80
    day, chart = await project(sessions, monkeypatch)
    assert day[metric] == value and day["sources"][metric]["source"] == code
    assert candidates(day, metric) == {code: value}
    if kind == "steps":
        assert chart == [80.0] + [None] * 23
    await store(sessions, kind, measurement(kind, field, "band", BAND, 60))
    day, _ = await project(sessions, monkeypatch)
    assert candidates(day, metric) == {"FITBIT": 3600 if kind == "sleep" else 60, code: value}
    assert day["sources"][metric]["source"] == "FITBIT"


def measurement(kind, field, name, source, value):
    if kind == "steps":
        return steps(name, value, source=source)
    if kind == "sleep":
        return sleep(name, source=source, summary={"minutesAsleep": value})
    return summary(kind, name, source=source, **{field: value})


@pytest.mark.parametrize("kind,metric,field,model", METRICS)
@pytest.mark.parametrize("state", ["deleted", "raw_quarantine", "normalized_quarantine", "held"])
async def test_eligibility_is_applied_before_platform_resolution(
    sessions, monkeypatch, kind, metric, field, model, state
):
    band = measurement(kind, field, "band", BAND, 60)
    phone = measurement(kind, field, "phone", PHONE, 80)
    await store(sessions, kind, band, phone)
    async with sessions.begin() as session:
        raws = (await session.scalars(select(RawHealthData))).all()
        old = next(row for row in raws if row.google_data_point_id == band["name"])
        if state == "deleted":
            old.source_deleted_at = T0
        elif state == "raw_quarantine":
            old.is_quarantined = True
        elif state == "normalized_quarantine":
            await session.execute(
                update(model)
                .where(model.google_data_point_id == band["name"])
                .values(is_quarantined=True)
            )
        else:
            new = next(row for row in raws if row.google_data_point_id == phone["name"])
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=old.id,
                    new_raw_id=new.id,
                    data_type=kind,
                    old_google_data_point_id=old.google_data_point_id,
                    new_google_data_point_id=new.google_data_point_id,
                    reason="duplicate",
                    old_fingerprint="a" * 64,
                    new_fingerprint="a" * 64,
                    same_reading=True,
                    new_payload_hash=new.payload_hash,
                )
            )
    day, _ = await project(sessions, monkeypatch)
    factor = 60 if kind == "sleep" else 1
    assert candidates(day, metric) == {"HEALTH_KIT": 80 * factor}
    day, _ = await project(sessions, monkeypatch, include_source_deleted=True)
    assert candidates(day, metric) == (
        {"FITBIT": 60 * factor, "HEALTH_KIT": 80 * factor}
        if state == "deleted"
        else {"HEALTH_KIT": 80 * factor}
    )
    if state == "held":
        async with sessions.begin() as session:
            await session.execute(
                update(RawHealthData)
                .where(RawHealthData.google_data_point_id == phone["name"])
                .values(source_deleted_at=T0)
            )
        day, _ = await project(sessions, monkeypatch)
        assert candidates(day, metric) == {"FITBIT": 60 * factor}


@pytest.mark.parametrize("zone_name,day,hours,shares", DST_DAYS)
@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT"])
async def test_dst_and_midnight_allocate_only_within_each_platform(
    sessions, monkeypatch, zone_name, day, hours, shares, preferred
):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(zone_name)
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    await store(
        sessions,
        "steps",
        steps("band", hours * 100, start, stop),
        steps("phone", hours * 200, start, stop, PHONE),
        steps("cross", 40, start - timedelta(minutes=30), start + timedelta(minutes=30)),
    )
    row, chart = await project(sessions, monkeypatch, preferred, day, zone.key)
    assert candidates(row, "steps") == {"FITBIT": hours * 100 + 20, "HEALTH_KIT": hours * 200}
    unit = 100 if preferred == "FITBIT" else 200
    expected = [float(unit)] * 24
    for hour, share in shares.items():
        expected[hour] = None if share is None else unit * share
    if preferred == "FITBIT":
        expected[0] += 20
    assert chart == expected


@pytest.mark.parametrize(
    "kind,metric,field,model,invalid",
    [(*case, "NaN") for case in METRICS]
    + [(*case, v) for case in METRICS[2:] for v in (-1, "-1", "-1e-999999", "-1e999999")],
)
async def test_missing_platform_value_and_measured_zero(
    sessions, monkeypatch, kind, metric, field, model, invalid
):
    await store(
        sessions,
        kind,
        measurement(kind, field, "band", BAND, 60),
        measurement(kind, field, "phone", PHONE, 80),
    )
    await store(sessions, kind, measurement(kind, field, "band", BAND, invalid))
    day, _ = await project(sessions, monkeypatch)
    assert candidates(day, metric) == {"HEALTH_KIT": 4800 if kind == "sleep" else 80}
    await store(sessions, kind, measurement(kind, field, "band", BAND, 0))
    day, chart = await project(sessions, monkeypatch)
    if kind == "steps":
        assert chart == [0.0] + [None] * 23
    assert day[metric] == 0 and day["sources"][metric]["source"] == "FITBIT"
    if kind == "sleep":
        assert day["sleep_assumptions"] == []


@pytest.mark.parametrize("kind,metric,field,model", METRICS)
async def test_corrected_platform_replaces_only_its_candidate(
    sessions, monkeypatch, clock, kind, metric, field, model
):
    original = measurement(kind, field, "band", BAND, 60)
    await store(sessions, kind, original, measurement(kind, field, "phone", PHONE, 80))
    corrected = measurement(kind, field, "band", {"platform": "GOOGLE_FIT"}, 90)
    clock.advance(seconds=1)
    await store(sessions, kind, corrected)
    day, chart = await project(sessions, monkeypatch)
    factor = 60 if kind == "sleep" else 1
    assert candidates(day, metric) == {"HEALTH_KIT": 80 * factor, "GOOGLE_FIT": 90 * factor}
    assert day[metric] == 80 * factor and day["sources"][metric]["source"] == "HEALTH_KIT"
    if kind == "steps":
        assert chart == [80.0] + [None] * 23
    async with sessions() as session:
        [version] = (await session.scalars(select(RawHealthDataVersion))).all()
        assert version.raw_json == original
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
        )
        assert raw.raw_json == corrected


async def test_previous_day_hours_do_not_fill_empty_end_date(sessions, monkeypatch):
    await store(sessions, "steps", steps("band", 100), steps("phone", 200, source=PHONE))
    day, chart = await project(sessions, monkeypatch, day=DAY + timedelta(days=1))
    assert day["steps"] is None and chart == [None] * 24
