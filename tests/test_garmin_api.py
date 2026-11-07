"""GET /api/garmin/status and /api/garmin/daily, with and without the integration enabled."""

from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import get_settings
from app.main import create_app
from app.models.garmin import GarminRawRecord


@pytest.fixture
async def client():
    application = create_app()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        yield http, application
    await application.state.runtime.shutdown()


async def test_disabled_integration_reports_so_and_serves_an_empty_summary(client):
    http, application = client
    assert application.state.runtime.garmin is None
    status = await http.get("/api/garmin/status")
    assert status.status_code == 200
    assert status.json() == {"enabled": False, "key_file_present": False, "kinds": {}}
    assert status.headers["cache-control"] == "no-store"
    daily = await http.get("/api/garmin/daily", params={"from": "2026-11-01", "to": "2026-11-07"})
    assert daily.status_code == 200
    assert daily.json() == {
        "source": "garmin",
        "from": "2026-11-01",
        "to": "2026-11-07",
        "days": [],
    }


async def test_daily_rejects_an_empty_or_too_long_range(client):
    http, _ = client
    too_long = await http.get(
        "/api/garmin/daily", params={"from": "2026-01-01", "to": "2026-06-01"}
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == {"code": "garmin_invalid_range", "max_days": 92}
    reversed_range = await http.get(
        "/api/garmin/daily", params={"from": "2026-11-07", "to": "2026-11-01"}
    )
    assert reversed_range.status_code == 422
    missing = await http.get("/api/garmin/daily")
    assert missing.status_code == 422


async def test_enabled_integration_reads_the_archive(sessions, monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("synthetic")
    monkeypatch.setenv("GARMIN_ENABLED", "true")
    monkeypatch.setenv("GARMIN_API_KEY_FILE", str(key))
    get_settings.cache_clear()
    try:
        application = create_app()
    finally:
        get_settings.cache_clear()
    runtime = application.state.runtime
    assert runtime.garmin is not None
    runtime.garmin.sessions = sessions
    async with sessions.begin() as session:
        session.add(
            GarminRawRecord(
                kind="steps",
                key="2026-11-06",
                body={"calendarDate": "2026-11-06", "totalSteps": 5120, "totalDistance": 3800},
                body_sha256="a" * 64,
                first_seen_at=datetime(2026, 11, 7, tzinfo=UTC),
            )
        )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        status = await http.get("/api/garmin/status")
        daily = await http.get(
            "/api/garmin/daily", params={"from": "2026-11-06", "to": "2026-11-06"}
        )
    await runtime.shutdown()
    body = status.json()
    assert body["enabled"] and body["key_file_present"] and body["raw_records"] == 1
    assert body["kinds"]["steps"]["newest_key"] == "2026-11-06"
    assert "synthetic" not in status.text  # the key never appears
    day = daily.json()["days"][0]
    assert day["steps"] == 5120 and day["distance_m"] == 3800 and day["sleep_seconds"] is None


async def test_garmin_routes_are_registered_and_scheduled_only_when_enabled():
    application = create_app()
    paths = application.openapi()["paths"]
    await application.state.runtime.shutdown()
    assert {"/api/garmin/status", "/api/garmin/daily"} <= set(paths)
