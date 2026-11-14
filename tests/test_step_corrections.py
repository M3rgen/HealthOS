"""Current Google payloads govern step values; retained normalized rows are history."""

import csv
import io
from datetime import datetime, timedelta
from itertools import product

import httpx
import pytest
from derived_records import DAY, PHONE, T0, snapshot, steps, store
from fastapi import FastAPI
from retrieval_records import Client, Runner
from sqlalchemy import select

from app.api import dashboard, sync
from app.core.config import Settings
from app.db.session import get_session
from app.google_health.client import Page
from app.models.health import Steps
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.synchronization import retrieval_coverage
from app.services.umay import codex_health

LAST = DAY + timedelta(days=2)
DATES = [(DAY + timedelta(days=i)).isoformat() for i in range(3)]
QUERY = {"start_date": DATES[0], "end_date": DATES[-1], "timezone": "UTC"}


@pytest.fixture
async def api(sessions, monkeypatch):
    class Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return (T0 + timedelta(days=3)).astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Now)
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.include_router(dashboard.router)
    app.include_router(sync.coverage_router)

    async def session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        yield client


async def outputs(api, sessions, expected):
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get("/api/dashboard", params={"end_date": LAST.isoformat(), "days": 3})
    assert response.status_code == 200
    data = response.json()
    assert [row["steps"] for row in reversed(data["days"])] == expected
    # Check the old day's hourly chart too, even after the raw interval moved/disappeared.
    for day, value in zip(DATES, expected, strict=True):
        chart = (await api.get("/api/dashboard", params={"end_date": day, "days": 1})).json()
        assert chart["charts"]["steps"] == [value, *([None] * 23)]
    history = (await api.get("/api/dashboard/history", params={"metric": "steps"})).json()
    by_day = {row["date"]: row["value"] for row in history["days"]}
    assert [by_day[day] for day in DATES] == expected
    response = await api.get("/api/dashboard/export.csv", params={"end_date": DATES[-1], "days": 3})
    exported = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert [row["steps"] for row in reversed(exported)] == [
        "" if value is None else str(value) for value in expected
    ]
    daily = await codex_health.read_health(sessions, {"action": "daily", **QUERY})
    assert [row["steps"] for row in reversed(daily["days"])] == expected
    card = await codex_health.read_health(
        sessions, {"action": "coverage", "data_type": "steps", **QUERY}
    )
    [metric] = card["metrics"]
    present = [day for day, value in zip(DATES, expected, strict=True) if value is not None]
    assert metric["value_days"] == metric["sampling"]["sample_count"] == len(present)
    assert metric["days_without_value"] == 3 - len(present)
    assert sorted(row["first_date"] for row in metric["source_regimes"]) == present
    assert all(row["value_days"] == row["record_count"] == 1 for row in metric["source_regimes"])
    response = await api.get("/api/sync/coverage", params={**QUERY, "data_type": "steps"})
    assert response.status_code == 200, response.text
    [kind] = response.json()["types"]
    assert [row["has_value"] for row in kind["days"]] == [v is not None for v in expected]
    # Completed retrieval remains proof of the fetch, even when a correction has no value.
    assert [row["retrieval"] for row in kind["days"]] == [
        "completed_with_records",
        "completed_empty",
        "completed_with_records",
    ]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    ("count", "interval"),
    list(product((None, "invalid", True), ("same", "moved", "lost")))
    + [(120, loss) for loss in ("startTime", "endTime", "lost")],
)
async def test_invalid_correction_and_recovery_across_all_readers(api, sessions, count, interval):
    original = steps(count=120)
    sound = steps("phone", count=30, start=T0 + timedelta(days=2), source=PHONE)
    await Runner(sessions, "sync", "steps", T0, T0 + timedelta(days=3)).run(
        Client(Page([original, sound], None))
    )
    await outputs(api, sessions, [120, None, 30])
    corrected = steps(count=count, start=T0 + timedelta(days=interval == "moved"))
    if count is None:
        corrected["steps"].pop("count")  # truly absent, rather than JSON null
    if interval == "lost":
        corrected["steps"].pop("interval")
    elif interval in {"startTime", "endTime"}:
        corrected["steps"]["interval"].pop(interval)
    stats = await store(sessions, "steps", corrected)
    assert stats.updated == stats.versions_kept == 1
    # A start-only payload takes the normalizer's generic scalar fallback; Steps stays old.
    assert stats.normalization_skipped == (interval != "endTime")
    async with sessions() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
        )
        assert raw.raw_json == corrected
        old = await session.scalar(
            select(Steps).where(Steps.google_data_point_id == raw.google_data_point_id)
        )
        assert old.count == 120 and old.start_time.date() == DAY
        [version] = list(await session.scalars(select(RawHealthDataVersion)))
        assert version.raw_json == original and version.version == 1
    await outputs(api, sessions, [None, None, 30])
    # Both aliases supported by normalization, including measured zero, recover normally.
    for alias, value in (("countSum", 0), ("steps", 45)):
        recovery = steps(start=T0 + timedelta(days=1))
        recovery["steps"] = {"interval": recovery["steps"]["interval"], alias: value}
        await store(sessions, "steps", recovery)
        await outputs(api, sessions, [None, value, 30])
    async with sessions() as session:
        versions = list(await session.scalars(select(RawHealthDataVersion).order_by("version")))
        assert [row.raw_json for row in versions[:2]] == [original, corrected]
        assert [row.version for row in versions] == [1, 2, 3]


async def test_coverage_read_budget_follows_current_interval(api, sessions, monkeypatch):
    await store(sessions, "steps", steps())
    await store(sessions, "steps", steps(count=None, start=T0 + timedelta(days=1)))
    monkeypatch.setattr(retrieval_coverage, "MAX_RECORDS", 0)
    for day, status in ((DATES[0], 200), (DATES[1], 413)):
        response = await api.get(
            "/api/sync/coverage",
            params={**QUERY, "data_type": "steps", "start_date": day, "end_date": day},
        )
        assert response.status_code == status, response.text
        if status == 413:
            assert response.json()["detail"]["code"] == "coverage_record_limit"
        else:
            assert response.json()["types"][0]["days"][0]["has_value"] is False


async def test_exclusions_follow_the_current_interval_of_a_moved_correction(api, sessions):
    await store(sessions, "steps", steps(count=120))
    # Invalid correction moved to the next day: the retained Steps row keeps the old day.
    await store(sessions, "steps", steps(count=None, start=T0 + timedelta(days=1)))
    async with sessions.begin() as session:
        row = await session.scalar(select(Steps))
        assert row.start_time.date() == DAY
        row.is_quarantined = True
    for day, count in ((DATES[0], 0), (DATES[1], 1), (DATES[2], 0)):
        response = await api.get(
            "/api/sync/coverage",
            params={**QUERY, "data_type": "steps", "start_date": day, "end_date": day},
        )
        assert response.status_code == 200, response.text
        [kind] = response.json()["types"]
        assert kind["exclusions"]["quarantined"] == count, day
        assert kind["days"][0]["has_value"] is False


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("field", ["count", "countSum", "steps", "count+alias", "countSum+alias"])
@pytest.mark.parametrize("value", [-1, "-1", "-1e-1000"])
async def test_negative_arrival_correction_and_recovery(api, sessions, initial, field, value):
    negative = steps(count=value)
    selected = field.split("+")[0]
    negative["steps"][selected] = negative["steps"].pop("count")
    if "+alias" in field:
        negative["steps"]["steps"] = 900
    original = steps(count=120) if initial else negative
    sound = steps("phone", count=30, start=T0 + timedelta(days=2), source=PHONE)
    records = [original, sound]
    await Runner(sessions, "sync", "steps", T0, T0 + timedelta(days=3)).run(
        Client(Page(records, None))
    )
    if initial:
        await outputs(api, sessions, [120, None, 30])
        stats = await store(sessions, "steps", negative)
        assert stats.normalization_skipped == stats.updated == stats.versions_kept == 1
    await outputs(api, sessions, [None, None, 30])
    async with sessions() as session:
        raw = await session.scalar(
            select(RawHealthData).where(RawHealthData.google_data_point_id == original["name"])
        )
        retained = await session.scalar(
            select(Steps).where(Steps.google_data_point_id == original["name"])
        )
        assert raw.raw_json == negative
        assert (retained.count if retained else None) == (120 if initial else None)
    for zero in (0, -0.0, "-0", "-0e-1000"):
        recovery = steps(count=zero)
        recovery["steps"][selected] = recovery["steps"].pop("count")
        assert (await store(sessions, "steps", recovery)).normalization_skipped == 0
        await outputs(api, sessions, [0, None, 30])
    await store(sessions, "steps", steps(count=45))
    await outputs(api, sessions, [45, None, 30])
    async with sessions() as session:
        versions = list(await session.scalars(select(RawHealthDataVersion).order_by("version")))
        assert [row.raw_json for row in versions[: 1 + initial]] == (
            [original, negative] if initial else [negative]
        )


@pytest.mark.parametrize(
    "loss,counts",
    [
        (loss, None)
        for loss in ("startTime", "endTime", "interval", "invalid-end", "equal", "reverse")
    ]
    + [
        ("count", counts)
        for counts in (
            {},
            {"count": None},
            {"count": "invalid"},
            {"count": True},
            {"count": False},
            {"count": "NaN"},
            {"count": "sNaN"},
            {"count": "Infinity"},
            {"count": "-Infinity"},
            {"count": "1e999999"},
            {"count": "-1e1000"},
            {"count": -1, "countSum": 900},
            {"countSum": "-1e-999999", "steps": 900},
            {"steps": "-1e-1000"},
        )
    ],
)
@pytest.mark.parametrize("sound", [False, True])
async def test_invalid_old_measurement_cannot_expand_history(api, sessions, loss, counts, sound):
    old = T0 - timedelta(days=65)
    original = steps(start=old)
    await store(sessions, "steps", original, *([steps("sound", start=T0)] if sound else []))

    async def history(earliest, latest, pages):
        async with sessions() as session:
            before = await snapshot(session)
        response = await api.get("/api/dashboard/history", params={"metric": "steps"})
        assert response.status_code == 200, response.text
        data = response.json()
        assert (data["earliest_date"], data["latest_date"], data["total_pages"]) == (
            earliest,
            latest,
            pages,
        )
        async with sessions() as session:
            assert await snapshot(session) == before
        if pages == 1:
            response = await api.get(
                "/api/dashboard/history", params={"metric": "steps", "page": 3}
            )
            assert response.status_code == 422
            assert response.json()["detail"] == "Bu geçmiş sayfası bulunamadı."

    await history(str(old.date()), str(DAY if sound else old.date()), 3)
    corrected = steps(start=old)
    interval = corrected["steps"]["interval"]
    if loss == "count":
        corrected["steps"] = {"interval": interval, **counts}
    elif loss == "interval":
        corrected["steps"].pop("interval")
    elif loss in {"startTime", "endTime"}:
        interval.pop(loss)
    else:
        interval["endTime"] = (
            "invalid"
            if loss == "invalid-end"
            else (old - timedelta(hours=loss == "reverse")).isoformat()
        )
    await store(sessions, "steps", corrected)
    await history(str(DAY) if sound else None, str(DAY) if sound else None, 1)
    async with sessions() as session:
        retained = await session.scalar(select(Steps).where(Steps.start_time < T0))
        assert retained.start_time.date() == old.date() and retained.count == 120
        assert list(await session.scalars(select(RawHealthDataVersion.raw_json))) == [original]
    recovery = T0 - timedelta(days=35)
    await store(sessions, "steps", steps(start=recovery, count=0))
    await history(str(recovery.date()), str(DAY if sound else recovery.date()), 2)
    page = (await api.get("/api/dashboard/history", params={"metric": "steps", "page": 2})).json()
    assert next(row["value"] for row in page["days"] if row["date"] == str(recovery.date())) == 0
