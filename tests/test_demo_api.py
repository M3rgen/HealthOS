"""The interface's sample-data routes: CSRF-guarded writes, status, refusal, removal."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app.main as main
from app.core.config import Settings
from app.core.providers import GARMIN
from app.core.request_limits import WriteLimits
from app.db.session import get_session
from app.models.health import Steps

JOB = {"X-HealthOS-Request": "1"}


@pytest.fixture
async def client(sessions, monkeypatch):
    config = Settings(_env_file=None, healthos_sources="none")
    runtime = SimpleNamespace(webhook_status=SimpleNamespace(record=AsyncMock()))
    monkeypatch.setattr(main, "get_settings", lambda: config)
    monkeypatch.setattr(main, "build_runtime", lambda *args: runtime)
    application = main.create_app()
    application.state.write_limits = WriteLimits(config)

    async def session():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://localhost:8000"
    ) as client:
        yield client


async def test_load_status_and_removal_round_trip(client):
    assert (await client.get("/api/demo")).json()["loaded"] is False
    loaded = await client.post("/api/demo?timezone=Europe/Istanbul", headers=JOB)
    assert loaded.status_code == 200, loaded.text
    body = loaded.json()
    assert body["loaded"] is True and body["added"]["steps"] == 90
    status = await client.get("/api/demo")
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["rows"] == body["rows"] > 0
    workouts = (await client.get("/api/dashboard/workouts")).json()
    assert workouts and {row["source_code"] for row in workouts} == {"DEMO"}
    removed = await client.delete("/api/demo", headers=JOB)
    assert removed.status_code == 200
    assert sum(removed.json()["removed"].values()) == body["rows"]
    assert (await client.get("/api/demo")).json() == {
        "loaded": False,
        "rows": 0,
        "first_day": None,
        "last_day": None,
    }


@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize(
    "headers",
    [{}, {**JOB, "Origin": "http://attacker.example"}, {**JOB, "Sec-Fetch-Site": "cross-site"}],
)
async def test_writes_need_a_deliberate_same_origin_request(client, method, headers):
    response = await getattr(client, method)("/api/demo", headers=headers)
    assert response.status_code == 403
    assert (await client.get("/api/demo")).json()["rows"] == 0


async def test_unknown_time_zone_is_rejected(client):
    response = await client.post("/api/demo?timezone=Mars/Olympus", headers=JOB)
    assert response.status_code == 422


async def test_archive_with_device_data_is_refused(client, sessions):
    async with sessions.begin() as session:
        session.add(
            Steps(
                google_data_point_id="garmin:steps:2026-11-10:v2",
                provider=GARMIN,
                source_record_id="steps:2026-11-10",
                start_time=datetime(2026, 11, 10, tzinfo=UTC),
                end_time=datetime(2026, 11, 11, tzinfo=UTC),
                count=4321,
            )
        )
    response = await client.post("/api/demo", headers=JOB)
    assert response.status_code == 409
    assert "gerçek veri" in response.json()["detail"]
    assert (await client.get("/api/demo")).json()["rows"] == 0
