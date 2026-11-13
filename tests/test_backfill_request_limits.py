from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from app.api import backfill, sync
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.db.session import get_session
from app.services.synchronization.sync_jobs import BackfillJobs


def _closed(coroutine):
    coroutine.close()
    return SimpleNamespace(done=lambda: True, add_done_callback=lambda callback: None)


async def test_backfill_and_sync_share_minute_budget_and_status_bypasses_it(monkeypatch):
    now = 0
    app = FastAPI()
    app.state.settings = settings = Settings(_env_file=None, manual_write_requests_per_minute=2)
    app.state.write_limits = WriteLimits(settings, clock=lambda: now)
    runtime = app.state.runtime = SimpleNamespace(
        backfill=SimpleNamespace(specs={"steps": object()}, run=AsyncMock()),
        incremental=SimpleNamespace(specs={"steps": object()}),
        sync_jobs=SimpleNamespace(start=Mock(return_value=SimpleNamespace(sequence=0, job_id="j"))),
        spawn=Mock(side_effect=lambda coroutine: _closed(coroutine)),
        backfill_jobs=BackfillJobs(),
    )
    app.include_router(backfill.router)
    app.include_router(sync.router)
    app.dependency_overrides[get_session] = lambda: None
    monkeypatch.setattr(backfill, "backfill_status", AsyncMock(return_value=[]))
    headers = {"X-HealthOS-Request": "1"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as http:
        response = await http.post("/backfill/start?data_type=steps", headers=headers)
        assert response.status_code == 202 and response.json()["data_types"] == ["steps"]
        assert (await http.post("/sync/all", headers=headers)).status_code == 202
        now = 10.5

        async def unread():
            pytest.fail("budget rejection must precede body reading")
            yield b"unread"

        for path in ("/backfill/start", "/sync/all"):
            response = await http.post(path, headers=headers, content=unread())
            assert response.status_code == 429 and response.headers["Retry-After"] == "50"
        assert (await http.get("/backfill/status")).status_code == 200
        runtime.spawn.assert_called_once()
        runtime.backfill.run.assert_called_once_with(["steps"])
        runtime.sync_jobs.start.assert_called_once()
        now = 60
        assert (await http.post("/backfill/start", headers=headers)).status_code == 202
        runtime.backfill.run.assert_called_with(None)


@pytest.mark.parametrize("headers, expected", [({}, 403), ({"X-HealthOS-Request": "1"}, 404)])
async def test_backfill_preserves_csrf_and_data_type_validation(headers, expected):
    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    app.state.write_limits = WriteLimits(app.state.settings)
    app.state.runtime = SimpleNamespace(backfill=SimpleNamespace(specs={}), spawn=Mock())
    app.include_router(backfill.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as http:
        response = await http.post("/backfill/start?data_type=unknown", headers=headers)
    assert response.status_code == expected
    app.state.runtime.spawn.assert_not_called()
