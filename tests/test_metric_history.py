from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import garmin_records as garmin_payloads
import httpx
import pytest
import pytest_asyncio
from derived_records import sleep, snapshot, steps, store
from fastapi import FastAPI
from history_cache_archive import REVISION_READS
from numeric_records import store_read_correction

import app.api.dashboard as dashboard_api
import app.services.dashboard_views.dashboard as dashboard_service
from app.db.session import get_session
from app.models.health import SleepSession, Steps
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard_history import history_data

GARMIN_STEPS = garmin_payloads.daily_steps(date(2000, 1, 1))["totalSteps"]
GARMIN_HEART = garmin_payloads.heart_rate(date(2000, 1, 1))["restingHeartRate"]
GARMIN_SLEEP = garmin_payloads.sleep(date(2000, 1, 1))
GARMIN_ASLEEP = GARMIN_SLEEP["sleepTimeSeconds"] - GARMIN_SLEEP["awakeSleepSeconds"]


@pytest.mark.parametrize("sound", [False, True])
async def test_overflowing_step_day_does_not_expand_history(client, sessions, sound):
    old = datetime(2026, 7, 1, tzinfo=UTC)
    await store_read_correction(sessions, "steps", steps("a", 1e308, old), steps("b", 1e308, old))
    if sound:
        await store(sessions, "steps", steps("recent", start=old + timedelta(days=90)))
    async with sessions() as session:
        before = await snapshot(session)
    response = await client.get("/api/dashboard/history", params={"metric": "steps"})
    assert response.status_code == 200
    data = response.json()
    expected = "2026-09-29" if sound else None
    assert (data["earliest_date"], data["latest_date"], data["total_pages"]) == (
        expected,
        expected,
        1,
    )
    response = await client.get("/api/dashboard/history", params={"metric": "steps", "page": 2})
    assert response.status_code == 422
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "zone,left,right,today,expected",
    [
        ("UTC", "09-28T23:00", "09-29T01:00", "09-30", [("09-28", 60), ("09-29", 60)]),
        ("UTC", "09-28T00:00", "09-30T00:00", "09-30", [("09-28", 1440), ("09-29", 1440)]),
        ("UTC", "09-28T23:59:59.999999", "09-29T00:00", "09-30", [("09-28", 0)]),
        (
            "UTC",
            "09-28T23:59:59.999999",
            "09-29T00:00:00.000001",
            "09-30",
            [("09-28", 0), ("09-29", 0)],
        ),
        (
            "UTC",
            "09-28T23:00",
            "10-02T00:00",
            "09-30",
            [("09-28", 60), ("09-29", 1440), ("09-30", 1440)],
        ),
        (
            "America/Los_Angeles",
            "09-29T06:00",
            "09-29T08:00",
            "09-30",
            [("09-28", 60), ("09-29", 60)],
        ),
        ("America/Los_Angeles", "09-29T06:00", "09-29T07:00", "09-30", [("09-28", 60)]),
        ("Europe/Brussels", "03-28T23:00", "03-29T22:00", "03-30", [("03-29", 1380)]),
        ("Europe/Brussels", "10-24T22:00", "10-25T23:00", "10-26", [("10-25", 1500)]),
    ],
)
@pytest.mark.parametrize(
    "counts",
    [None, {"count": 0, "countSum": 900}, {"countSum": "-0", "steps": 900}, {"steps": "-0e-1000"}],
)
async def test_step_bounds_match_ingested_contribution_days(
    sessions, zone, left, right, today, expected, counts
):
    start, end = (datetime.fromisoformat(f"2026-{stamp}+00:00") for stamp in (left, right))
    tomorrow = datetime.combine(
        date.fromisoformat(f"2026-{today}") + timedelta(days=1), datetime.min.time(), ZoneInfo(zone)
    ).astimezone(UTC)
    record = steps(count=(end - start).total_seconds() / 60, start=start, end=end)
    if counts is not None:
        record["steps"] = {"interval": record["steps"]["interval"], **counts}
    await store(
        sessions,
        "steps",
        record,
        steps("future", start=tomorrow),
    )
    async with sessions() as session:
        before = await snapshot(session)
        page = await history_data(session, "steps", 1, zone, date.fromisoformat(f"2026-{today}"))
        values = {
            str(row["date"]): row["value"] for row in page["days"] if row["value"] is not None
        }
        assert values == {
            f"2026-{day}": 0 if counts is not None else value for day, value in expected
        }
        assert str(page["earliest_date"]) == min(values)
        assert str(page["latest_date"]) == max(values)
        assert await snapshot(session) == before


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 9, 30, 0, 30, tzinfo=UTC)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(dashboard_api, "datetime", FixedDateTime)
    monkeypatch.setattr(dashboard_service, "datetime", FixedDateTime)


@pytest_asyncio.fixture
async def client(sessions):
    application = FastAPI()
    application.include_router(dashboard_api.router)

    async def override_session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as connection:
        yield connection


def records(metric, day, identifier, *, quarantined=False, deleted=False, normalized_bad=False):
    stamp = datetime.combine(day, datetime.min.time(), UTC)
    kind = {"steps": "steps", "sleep": "sleep", "heart": "daily-resting-heart-rate"}[metric]
    start, end = stamp + timedelta(hours=12), stamp + timedelta(hours=13)
    normalized = []
    if metric == "steps":
        normalized = [
            Steps(
                google_data_point_id=identifier,
                start_time=start,
                end_time=end,
                count=600,
                is_quarantined=normalized_bad,
            )
        ]
    elif metric == "sleep":
        start, end = stamp - timedelta(hours=2), stamp + timedelta(hours=6)
        normalized = [
            SleepSession(
                google_data_point_id=identifier,
                start_time=start,
                end_time=end,
                duration_seconds=28800,
                awake_seconds=1800,
                is_quarantined=normalized_bad,
            )
        ]
    else:
        start = end = stamp
    raw = RawHealthData(
        google_data_point_id=identifier,
        data_type=kind,
        source_start_time=start,
        source_end_time=end,
        raw_json=steps(count=600, start=start, end=end)
        if metric == "steps"
        else sleep(start=start, end=end, summary={"minutesAsleep": 450})
        if metric == "sleep"
        else {"dailyRestingHeartRate": {"date": day.isoformat(), "beatsPerMinute": 71}},
        payload_hash="test",
        first_seen_at=stamp,
        last_seen_at=stamp,
        is_quarantined=quarantined,
        source_deleted_at=stamp if deleted else None,
    )
    return [raw, *normalized]


@pytest.mark.parametrize("metric,expected", [("steps", 600), ("sleep", 27000), ("heart", 71)])
async def test_month_pages_cover_full_archive_and_filter_sources(
    client, sessions, metric, expected
):
    async with sessions() as session:
        session.add_all(
            records(metric, date(2026, 8, 12), "old")
            + records(metric, date(2026, 9, 29), "current")
            + records(metric, date(2020, 1, 1), "quarantined", quarantined=True)
            + records(metric, date(2019, 1, 1), "deleted", deleted=True)
            + records(metric, date(2026, 9, 29), "deleted-recent", deleted=True)
            + records(metric, date(2026, 9, 29), "quarantined-recent", quarantined=True)
            + records(metric, date(2099, 1, 1), "future")
        )
        await session.commit()
    first = (await client.get("/api/dashboard/history", params={"metric": metric})).json()
    assert first["start_date"] == "2026-09-01"
    assert first["end_date"] == "2026-09-30"
    assert first["earliest_date"] == "2026-08-12"
    assert first["latest_date"] == "2026-09-29"
    assert first["total_pages"] == 2
    assert len(first["days"]) == 30
    assert first["days"][0] == {"date": "2026-09-30", "value": None, "partial": True}
    assert first["days"][1]["value"] == expected
    second = (
        await client.get("/api/dashboard/history", params={"metric": metric, "page": 2})
    ).json()
    assert second["start_date"] == "2026-08-02"
    assert second["end_date"] == "2026-08-31"
    assert len(second["days"]) == 30
    assert not any(row["partial"] for row in second["days"])
    assert {row["date"] for row in first["days"]}.isdisjoint(row["date"] for row in second["days"])
    assert next(row for row in second["days"] if row["date"] == "2026-08-12")["value"] == expected
    assert (
        await client.get("/api/dashboard/history", params={"metric": metric, "page": 3})
    ).status_code == 422


@pytest.mark.parametrize("metric", ["steps", "sleep", "heart"])
async def test_empty_archive_has_one_full_empty_page(client, metric):
    response = await client.get("/api/dashboard/history", params={"metric": metric})
    assert response.status_code == 200
    data = response.json()
    assert data["total_pages"] == data["page"] == 1
    assert data["earliest_date"] is data["latest_date"] is None
    assert len(data["days"]) == 30
    assert all(row["value"] is None for row in data["days"])


@pytest.mark.parametrize(
    "counts,value",
    [
        ({"count": "bad", "countSum": 7, "steps": 9}, 7),
        ({"count": True, "countSum": None, "steps": 9}, 9),
        ({"count": 0, "countSum": -1, "steps": 9}, 0),
        ({"countSum": "-0", "steps": -1}, 0),
        ({"count": "NaN", "countSum": 7}, None),
        ({"count": "-1e-999999", "countSum": 7}, None),
    ],
)
async def test_corrected_latest_step_uses_first_parseable_count(client, sessions, counts, value):
    latest = datetime(2026, 9, 29, tzinfo=UTC)
    middle = latest - timedelta(days=3)
    await store(sessions, "steps", steps("middle", start=middle), steps("latest", start=latest))
    corrected = steps("latest", start=latest)
    corrected["steps"] = {"interval": corrected["steps"]["interval"], **counts}
    await store(sessions, "steps", corrected)
    async with sessions() as session:
        before = await snapshot(session)
    response = await client.get("/api/dashboard/history", params={"metric": "steps"})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["earliest_date"] == str(middle.date())
    assert page["latest_date"] == str((latest if value is not None else middle).date())
    assert page["days"][1]["value"] == value
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("metric", ["steps", "sleep"])
async def test_normalized_quarantine_does_not_expand_or_pollute_history(client, sessions, metric):
    async with sessions() as session:
        session.add_all(
            records(metric, date(2026, 9, 29), "current")
            + records(metric, date(2019, 1, 1), "old-bad", normalized_bad=True)
            + records(metric, date(2026, 9, 29), "new-bad", normalized_bad=True)
        )
        await session.commit()
    data = (await client.get("/api/dashboard/history", params={"metric": metric})).json()
    assert data["total_pages"] == 1
    assert data["earliest_date"] == "2026-09-29"
    assert data["days"][1]["value"] == (600 if metric == "steps" else 27000)


async def test_archive_has_no_history_cutoff_and_uses_requested_civil_days(client, sessions):
    async with sessions() as session:
        session.add_all(records("steps", date(2020, 1, 1), "old"))
        await session.commit()
    params = {"metric": "steps", "timezone": "America/Los_Angeles"}
    data = (await client.get("/api/dashboard/history", params=params)).json()
    today = date(2026, 9, 29)  # Still the previous day west of UTC.
    assert data["end_date"] == today.isoformat()
    assert data["total_pages"] == (today - date(2020, 1, 1)).days // 30 + 1
    last = (
        await client.get("/api/dashboard/history", params={**params, "page": data["total_pages"]})
    ).json()
    assert next(row for row in last["days"] if row["date"] == "2020-01-01")["value"] == 600


async def test_daily_heart_date_is_not_shifted_in_western_timezone(client, sessions):
    async with sessions() as session:
        session.add_all(records("heart", date(2026, 9, 29), "daily"))
        await session.commit()
    data = (
        await client.get(
            "/api/dashboard/history", params={"metric": "heart", "timezone": "America/Los_Angeles"}
        )
    ).json()
    assert data["earliest_date"] == data["latest_date"] == "2026-09-29"
    assert data["days"][0]["value"] == 71


@pytest.mark.parametrize(
    "params",
    [
        {"metric": "weight"},
        {"metric": "steps", "page": 0},
        {"metric": "steps", "page": 2},
        {"metric": "steps", "page": "one"},
        {"metric": "steps", "timezone": "wrong"},
        {},
    ],
)
async def test_history_validation(client, params):
    assert (await client.get("/api/dashboard/history", params=params)).status_code == 422


@pytest.mark.parametrize("metric", ["steps", "sleep", "heart"])
async def test_oldest_history_window_is_checked_before_reading(
    dashboard_dates, monkeypatch, metric
):
    client, statements = dashboard_dates
    monkeypatch.setattr(dashboard_api, "datetime", FixedDateTime)
    monkeypatch.setattr(dashboard_service, "datetime", FixedDateTime)
    params = {"metric": metric}
    first = await client.get("/api/dashboard/history", params=params)
    assert first.status_code == 200, first.text
    pages = first.json()["total_pages"]
    assert pages == (date(2026, 9, 30) - date.min).days // 30 + 1
    assert first.json()["earliest_date"] == "0001-01-01"
    previous = await client.get("/api/dashboard/history", params={**params, "page": pages - 1})
    assert previous.status_code == 200, previous.text
    assert len(previous.json()["days"]) == 30
    for page in (pages, pages + 1, 10**100):
        statements.clear()
        response = await client.get("/api/dashboard/history", params={**params, "page": page})
        assert response.status_code == 422, response.text
        assert statements == []
        assert response.json() == {
            "detail": "Seçilen tarih aralığı bu saat diliminde hesaplanamıyor."
        }


@pytest.mark.parametrize("metric", ["steps", "sleep"])
async def test_history_archive_date_conversion_overflow(dashboard_dates, monkeypatch, metric):
    client, statements = dashboard_dates
    monkeypatch.setattr(dashboard_api, "datetime", FixedDateTime)
    monkeypatch.setattr(dashboard_service, "datetime", FixedDateTime)
    response = await client.get(
        "/api/dashboard/history", params={"metric": metric, "timezone": "Etc/GMT+12"}
    )
    assert response.status_code == 422, response.text
    assert len(statements) == 2  # One visibility probe, then the original failing bounds read.
    assert statements[0] in REVISION_READS
    assert "raw_health_data" in statements[1]
    assert response.json() == {"detail": "Seçilen tarih aralığı bu saat diliminde hesaplanamıyor."}


@pytest.mark.parametrize(
    "today,zone,status",
    [(date(1, 1, 30), "UTC", 200), (date(1, 1, 30), "Etc/GMT-14", 422), (date.max, "UTC", 422)],
)
async def test_history_clock_boundary(dashboard_dates, monkeypatch, today, zone, status):
    client, statements = dashboard_dates

    class BoundaryDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(today, datetime.min.time(), UTC).astimezone(tz)

    monkeypatch.setattr(dashboard_api, "datetime", BoundaryDateTime)
    monkeypatch.setattr(dashboard_service, "datetime", BoundaryDateTime)
    response = await client.get(
        "/api/dashboard/history", params={"metric": "steps", "timezone": zone}
    )
    assert response.status_code == status, response.text
    if status == 422:
        assert statements == []
    else:
        assert response.json()["start_date"] == "0001-01-01"
        assert response.json()["days"][-1]["value"] == 240


def garmin_records(metric, day, identifier=None, *, quarantined=False, wake=None):
    """Synthetic normalized Garmin rows with the same calendar keys as ingestion."""
    from app.models.health import RestingHeartRate

    stamp = datetime.combine(day, datetime.min.time(), UTC)
    kind = {"steps": "steps", "sleep": "sleep", "heart": "resting-heart-rate"}[metric]
    common = {
        "google_data_point_id": identifier or f"garmin:{kind}:{day}",
        "source_record_id": f"{kind}:{day}",
        "provider": "GARMIN",
        "is_quarantined": quarantined,
    }
    if metric == "heart":
        return [RestingHeartRate(**common, timestamp=stamp, bpm=GARMIN_HEART)]
    end = wake or stamp + timedelta(hours=6)
    start = end - timedelta(seconds=GARMIN_SLEEP["sleepTimeSeconds"])
    if metric == "steps":
        return [
            Steps(
                **common, start_time=stamp, end_time=stamp + timedelta(days=1), count=GARMIN_STEPS
            )
        ]
    return [
        SleepSession(
            **common,
            start_time=start,
            end_time=end,
            duration_seconds=GARMIN_SLEEP["sleepTimeSeconds"],
            awake_seconds=GARMIN_SLEEP["awakeSleepSeconds"],
        )
    ]


@pytest.mark.parametrize(
    "metric,expected", [("steps", GARMIN_STEPS), ("sleep", GARMIN_ASLEEP), ("heart", GARMIN_HEART)]
)
async def test_garmin_only_archive_exposes_every_page(client, sessions, metric, expected):
    today = date(2026, 9, 30)
    oldest = today - timedelta(days=65)
    async with sessions.begin() as session:
        session.add_all(garmin_records(metric, today) + garmin_records(metric, oldest))
    params = {"metric": metric}
    first = await client.get("/api/dashboard/history", params=params)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["total_pages"] == 3
    assert data["earliest_date"] == oldest.isoformat()
    assert data["latest_date"] == today.isoformat()
    assert data["days"][0]["value"] == expected
    third = await client.get("/api/dashboard/history", params={**params, "page": 3})
    assert third.status_code == 200, third.text
    assert next(row for row in third.json()["days"] if row["date"] == oldest.isoformat()) == {
        "date": oldest.isoformat(),
        "value": expected,
        "partial": False,
    }
    assert third.json()["earliest_date"] == data["earliest_date"]
    assert third.json()["latest_date"] == data["latest_date"]
    assert (
        await client.get("/api/dashboard/history", params={**params, "page": 4})
    ).status_code == 422


@pytest.mark.parametrize("metric,expected", [("steps", 600), ("sleep", 27000), ("heart", 71)])
async def test_union_bounds_keep_same_day_source_precedence(client, sessions, metric, expected):
    today = date(2026, 9, 30)
    oldest, middle = today - timedelta(days=65), today - timedelta(days=10)
    if metric == "steps":
        # Valid ingestion followed by invalid counts at both archive edges must
        # leave Garmin's bounds intact, despite the retained Google Steps rows.
        for name, start in (("invalid-old", oldest - timedelta(days=30)), ("invalid-new", today)):
            start = datetime.combine(start, datetime.min.time(), UTC)
            await store(sessions, "steps", steps(name, start=start))
            await store(sessions, "steps", steps(name, count=-1, start=start))
    if metric == "sleep":
        for name, day in (("invalid-old", oldest - timedelta(days=30)), ("invalid-new", today)):
            start = datetime.combine(day, datetime.min.time(), UTC)
            await store(sessions, "sleep", sleep(name, start=start, end=start + timedelta(hours=8)))
            await store(
                sessions,
                "sleep",
                sleep(name, start=start, end=start + timedelta(hours=8), duration="-1s"),
            )
    async with sessions.begin() as session:
        google = records(metric, middle, "google")
        google[0].raw_json = {**google[0].raw_json, "dataSource": {"platform": "FITBIT"}}
        session.add_all(
            google
            + garmin_records(metric, oldest)
            + garmin_records(metric, middle)
            + garmin_records(metric, today)
        )
    first = (await client.get("/api/dashboard/history", params={"metric": metric})).json()
    assert first["earliest_date"] == oldest.isoformat()
    assert first["latest_date"] == today.isoformat()
    assert first["total_pages"] == 3
    assert (
        next(row for row in first["days"] if row["date"] == middle.isoformat())["value"] == expected
    )
    response = await client.get("/api/dashboard/history", params={"metric": metric, "page": 3})
    assert response.status_code == 200, response.text
    assert any(
        row["date"] == oldest.isoformat() and row["value"] is not None
        for row in response.json()["days"]
    )


@pytest.mark.parametrize("metric", ["steps", "sleep", "heart"])
async def test_garmin_bounds_exclude_quarantine_superseded_versions_and_future(
    client, sessions, metric
):
    current = date(2026, 9, 29)
    async with sessions.begin() as session:
        session.add_all(
            garmin_records(metric, date(2010, 1, 1), "replaced")
            + garmin_records(metric, current, "replaced:v2")
            + garmin_records(metric, date(2011, 1, 1), "bad", quarantined=True)
            + garmin_records(metric, date(2012, 1, 1), "held")
            + garmin_records(metric, current, "held:v2", quarantined=True)
            + garmin_records(metric, date(2026, 9, 30), "latest-bad", quarantined=True)
            + garmin_records(metric, date(2026, 10, 1), "future")
        )
    data = (await client.get("/api/dashboard/history", params={"metric": metric})).json()
    assert data["earliest_date"] == data["latest_date"] == current.isoformat()
    assert data["total_pages"] == 1
    assert sum(row["value"] is not None for row in data["days"]) == 1


@pytest.mark.parametrize("metric,expected", [("steps", GARMIN_STEPS), ("heart", GARMIN_HEART)])
async def test_garmin_daily_bounds_keep_record_calendar_day_in_west(
    client, sessions, metric, expected
):
    day = date(2026, 9, 29)
    async with sessions.begin() as session:
        session.add_all(
            garmin_records(metric, day) + garmin_records(metric, day + timedelta(days=1))
        )
    response = await client.get(
        "/api/dashboard/history", params={"metric": metric, "timezone": "America/Los_Angeles"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["earliest_date"] == data["latest_date"] == day.isoformat()
    assert data["days"][0]["date"] == day.isoformat()
    assert data["days"][0]["value"] == expected


@pytest.mark.parametrize(
    "wake,local_day",
    [
        ("2026-03-08T07:30:00+00:00", date(2026, 3, 7)),  # UTC day differs, before midnight.
        ("2026-03-08T08:00:00+00:00", date(2026, 3, 8)),  # Local midnight.
        ("2026-03-08T09:30:00+00:00", date(2026, 3, 8)),  # Before spring DST jump.
        ("2026-03-08T10:30:00+00:00", date(2026, 3, 8)),  # After spring DST jump.
        ("2026-11-01T08:30:00+00:00", date(2026, 11, 1)),  # First 01:30 in autumn.
        ("2026-11-01T09:30:00+00:00", date(2026, 11, 1)),  # Second 01:30 in autumn.
    ],
)
async def test_garmin_sleep_bounds_use_requested_wake_day(sessions, wake, local_day):
    from app.services.dashboard_views.dashboard_history import history_data

    wake = datetime.fromisoformat(wake)
    async with sessions.begin() as session:
        # Calendar keys deliberately differ from the local wake day.
        session.add_all(garmin_records("sleep", wake.date() - timedelta(days=1), wake=wake))
        session.add_all(
            garmin_records(
                "sleep", local_day + timedelta(days=1), "future", wake=wake + timedelta(days=2)
            )
        )
    async with sessions() as session:
        data = await history_data(session, "sleep", 1, "America/Los_Angeles", local_day)
    assert data["earliest_date"] == data["latest_date"] == local_day
    assert data["days"][0]["date"] == local_day.isoformat()
    assert data["days"][0]["value"] == GARMIN_ASLEEP


async def test_invalid_garmin_sleep_interval_cannot_establish_archive_day(client, sessions):
    invalid = garmin_records("sleep", date(2010, 1, 1))[0]
    invalid.start_time = invalid.end_time
    async with sessions.begin() as session:
        session.add(invalid)
    response = await client.get("/api/dashboard/history", params={"metric": "sleep"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["earliest_date"] is data["latest_date"] is None
    assert data["total_pages"] == 1
    assert all(row["value"] is None for row in data["days"])


async def test_garmin_sleep_archive_date_conversion_overflow(client, sessions):
    async with sessions.begin() as session:
        session.add_all(garmin_records("sleep", date.min, wake=datetime(1, 1, 1, 8, tzinfo=UTC)))
    response = await client.get(
        "/api/dashboard/history", params={"metric": "sleep", "timezone": "Etc/GMT+12"}
    )
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "Seçilen tarih aralığı bu saat diliminde hesaplanamıyor."}
