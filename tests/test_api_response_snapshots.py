"""Synthetic HTTP JSON captured before adding response models (#305).

Real SQLite service reads cover empty/populated archives, optional fields and
non-ASCII text. HTTP stays in-process; no sockets or external providers are used.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from support.clock import freeze_app_clock
from test_dashboard_characterization import seed

from app.api import (
    backfill,
    dashboard,
    devices,
    garmin,
    google_health,
    health,
    settings,
    sync,
    webhooks,
)
from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.db.session import get_session
from app.models.garmin import GarminRawRecord
from app.models.manual import ManualWorkout
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.runtime import build_runtime
from app.services.dashboard_views import dashboard_workouts
from app.services.readers import dashboard_manual
from app.services.synchronization import reconciliation_reports, sync_jobs

AT = datetime(2026, 9, 29, 12, tzinfo=UTC)
PATHS = [
    "/health/live",
    "/health",
    "/api/status",
    "/health/backup",
    "/api/dashboard?end_date=2026-09-29&days=3",
    "/api/dashboard/history?metric=steps",
    "/api/dashboard/workouts?month=2026-09-01",
    "/api/dashboard/measurements",
    "/api/settings/appearance",
    "/api/settings/display",
    "/api/devices",
    "/api/webhooks/status",
    "/api/garmin/status",
    "/api/garmin/daily?from=2026-09-28&to=2026-09-29",
    "/sync/status",
    "/backfill/status",
    "/api/sync/reconciliation",
    "/api/sync/coverage?start_date=2026-09-28&end_date=2026-09-29&timezone=UTC&data_type=steps",
    "/google-health/capabilities",
    "/google-health/devices",
]


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
async def test_frozen_http_json(sessions, monkeypatch, tmp_path, populated, unlocked_pull):
    config = Settings(
        _env_file=None,
        app_env="test",
        backup_status_file=tmp_path / "absent",
        # The default lives in the home directory; a snapshot must not see who runs it.
        garmin_api_key_file=tmp_path / "absent-garmin-key",
        # CI turns the watchdog off in its environment; the frozen responses have it on.
        watchdog_enabled=True,
    )
    runtime = build_runtime(config, sessions)
    runtime.webhook_status.started_at = AT
    monkeypatch.setattr(sync_jobs, "uuid4", lambda: UUID(int=13))

    def spawn(coroutine):
        coroutine.close()
        return asyncio.get_running_loop().create_future()

    monkeypatch.setattr(runtime, "spawn", spawn)

    async def create_measurement(session, values):
        return await dashboard_manual.create_measurement(session, {**values, "id": UUID(int=20)})

    async def create_workout(session, values):
        return await dashboard_workouts.create_workout(session, {**values, "id": UUID(int=21)})

    monkeypatch.setattr(dashboard, "create_measurement", create_measurement)
    monkeypatch.setattr(dashboard, "create_workout", create_workout)
    runtime.discovery.devices = AsyncMock(return_value=[])
    runtime.discovery.probe = AsyncMock(
        return_value=[
            {
                "data_type": "steps",
                "access_method": "list",
                "accessible": True,
                "has_recent_data": True,
                "sample_start": AT,
                "sample_end": None,
                "top_level_fields": ["name", "steps"],
            },
            {
                "data_type": "sleep",
                "access_method": "list",
                "accessible": False,
                "has_recent_data": False,
                "error_code": "discovery_probe_failed",
                "error": "Veri türüne erişim denetlenemedi. Daha sonra yeniden deneyin.",
            },
        ]
    )
    app = FastAPI()
    app.state.settings, app.state.runtime = config, runtime
    app.state.write_limits = WriteLimits(config)
    for module in (
        backfill,
        dashboard,
        devices,
        garmin,
        google_health,
        health,
        settings,
        sync,
        webhooks,
    ):
        app.include_router(module.router)
    app.include_router(sync.coverage_router)
    app.openapi()
    freeze_app_clock(monkeypatch, AT)

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[runtime_dep] = lambda: runtime
    app.dependency_overrides[settings_dep] = lambda: config
    if populated:
        await seed(sessions, AT.date(), "UTC")
        runtime.garmin = unlocked_pull(config, sessions)
        async with sessions.begin() as session:
            await session.execute(update(RawHealthData).values(updated_at=AT))
            session.add(
                GarminRawRecord(
                    kind="steps",
                    key="2026-09-29",
                    body={"totalSteps": 1234},
                    body_sha256="a" * 64,
                    first_seen_at=AT,
                )
            )
            session.add(
                SyncState(
                    data_type="steps",
                    status=JobStatus.completed,
                    records_downloaded=12,
                    last_successful_sync=AT,
                )
            )
            session.add(
                BackfillJob(
                    id=UUID(int=10),
                    data_type="steps",
                    window_start=AT,
                    window_end=AT,
                    status=JobStatus.failed,
                    last_error="TimeoutError",
                )
            )
            session.add(
                ManualWorkout(
                    id=UUID(int=11),
                    exercise_type="Yürüyüş",
                    start_time=AT,
                    end_time=AT.replace(hour=13),
                    created_at=AT,
                    updated_at=AT,
                )
            )
        await reconciliation_reports.begin_run(
            sessions, UUID(int=12), AT.replace(hour=11), AT, AT, ["steps"]
        )
        await reconciliation_reports.start_type(sessions, UUID(int=12), "steps")
        await reconciliation_reports.finish_type(sessions, UUID(int=12), "steps")
        await reconciliation_reports.finish_run(sessions, UUID(int=12))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            actual = {}
            for path in PATHS + (["/sync/status/steps"] if populated else []):
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)
                actual[path] = response.json()
            if populated:
                for action in ("flag", "flag", "unflag"):
                    path = f"/api/dashboard/measurements/{UUID(int=1)}/{action}"
                    response = await client.post(
                        path, json={"reason": "Sentetik kayıt"}, headers={"X-HealthOS-Request": "1"}
                    )
                    assert response.status_code in (200, 201), response.text
                    actual[f"{path}/{response.status_code}"] = response.json()
            writes = [
                ("PUT", "/api/settings/appearance", {"logo": "upload", "umay": None}),
                (
                    "PUT",
                    "/api/settings/display",
                    {
                        "schema_version": 1,
                        "preferences": {"motion": False, "timezone": "Europe/Paris"},
                    },
                ),
                (
                    "POST",
                    "/api/dashboard/measurements",
                    {
                        "metric": "weight",
                        "value": "71.25",
                        "measured_at": "2026-09-29T01:00:00+00:00",
                    },
                ),
                (
                    "POST",
                    "/api/dashboard/workouts",
                    {
                        "exercise_type": "Koşu",
                        "start_time": "2026-09-29T01:00:00Z",
                        "end_time": "2026-09-29T02:00:00Z",
                        "calories_kcal": "123.45",
                    },
                ),
                ("POST", "/sync/all", None),
                ("POST", "/sync/steps", None),
                ("POST", "/backfill/start", None),
            ]
            for method, path, body in writes:
                response = await client.request(
                    method, path, json=body, headers={"X-HealthOS-Request": "1"}
                )
                assert response.status_code in (200, 201, 202), (path, response.text)
                actual[f"{method} {path}"] = response.json()
            snapshot = (
                Path(__file__).with_name("fixtures")
                / f"api-responses-{'populated' if populated else 'empty'}.json"
            )
            expected = json.loads(snapshot.read_text())
            if populated:
                expected["/api/garmin/status"] = json.loads(
                    snapshot.with_name("api-garmin-status-populated.json").read_text()
                )
            assert json.dumps(actual, sort_keys=True, ensure_ascii=False) == json.dumps(
                expected, sort_keys=True, ensure_ascii=False
            )
    finally:
        await runtime.shutdown()
