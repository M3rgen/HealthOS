"""Status routes keep their archive projections after delegating queries to services."""

from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.api import sync
from app.api.deps import runtime as runtime_dep
from app.db.session import get_session
from app.models.state import JobStatus, SyncState


async def test_sync_status_list_detail_and_missing_type(sessions):
    application = FastAPI()
    application.include_router(sync.router)
    runtime = SimpleNamespace(incremental=SimpleNamespace(last_runs={"steps": {"pages": 2}}))
    application.dependency_overrides[runtime_dep] = lambda: runtime

    async def session_override():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session_override
    async with sessions.begin() as session:
        session.add_all(
            [
                SyncState(data_type="steps", status=JobStatus.completed, records_downloaded=12),
                SyncState(data_type="heart-rate", status=JobStatus.pending),
            ]
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        response = await client.get("/sync/status")
        assert response.status_code == 200
        rows = response.json()
        assert [row["data_type"] for row in rows] == ["heart-rate", "steps"]
        assert rows[0]["last_run"] is None
        detail = await client.get("/sync/status/steps")
        assert detail.status_code == 200
        assert (
            detail.json()
            == rows[1]
            == {
                "data_type": "steps",
                "status": "completed",
                "last_requested_from": None,
                "last_requested_to": None,
                "last_successful_sync": None,
                "records_downloaded": 12,
                "last_error": None,
                "last_run": {"pages": 2},
            }
        )
        missing = await client.get("/sync/status/sleep")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "No synchronization state for this data type"}
