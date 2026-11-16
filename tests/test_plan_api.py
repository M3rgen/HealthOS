"""Real middleware, authenticated plan reads and atomic UI-state writes."""

import httpx
import pytest
from sqlalchemy import select
from support.auth import BASE, TOKEN, build
from support.plans import assert_private_output, sample

from app.core.auth import COOKIE_NAME
from app.db.session import get_session
from app.models.plan import PlanState
from app.services.operations.plans import import_document

URL = "/api/plan"
JOB = {"X-HealthOS-Request": "1"}
STATE = {"start_date": "2026-01-01", "checked_item_ids": ["example-grain", "example-box"]}
EMPTY_STATE = {"start_date": None, "checked_item_ids": []}


@pytest.fixture
def application(monkeypatch, sessions):
    app = build(monkeypatch, app_public_url=BASE)

    async def override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override
    return app


def client(application, authenticated=True):
    cookies = {COOKIE_NAME: application.state.ui_sessions.issue()} if authenticated else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url=BASE, cookies=cookies
    )


async def test_plan_requires_session_or_token_and_never_caches(application):
    async with client(application, False) as http:
        for method, path in (("GET", URL), ("PUT", URL + "/state")):
            response = await http.request(method, path, json=STATE, headers=JOB)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
        response = await http.get(URL, headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200 and response.json() == {"plan": None}
        assert response.headers["cache-control"] == "no-store"


async def test_empty_plan_then_document_and_state_roundtrip(application, sessions, caplog):
    caplog.clear()
    async with client(application) as http:
        assert (await http.get(URL)).json() == {"plan": None}
        assert (await http.put(URL + "/state", json=EMPTY_STATE, headers=JOB)).status_code == 422
        async with sessions.begin() as session:
            version, _ = await import_document(session, sample())
        response = await http.get(URL)
        assert response.json()["plan"] == sample()
        assert response.json()["version"] == version.id
        assert response.json()["schema_version"] == 1
        assert response.json()["imported_at"]
        assert response.json()["state"] == EMPTY_STATE
        written = await http.put(URL + "/state", json=STATE, headers=JOB)
        assert written.status_code == 200 and written.json() == STATE
        assert written.headers["cache-control"] == "no-store"
        assert (await http.get(URL)).json()["state"] == STATE
        replaced = await http.put(URL + "/state", json=EMPTY_STATE, headers=JOB)
        assert replaced.json() == EMPTY_STATE
        assert (await http.get(URL)).json()["state"] == EMPTY_STATE
    async with sessions() as session:
        assert (await session.scalars(select(PlanState.id))).all() == [1]
    assert_private_output(caplog.text)


@pytest.mark.parametrize(
    "payload",
    [
        {**STATE, "checked_item_ids": ["unknown"]},
        {**STATE, "checked_item_ids": ["example-grain", "example-grain"]},
        {**STATE, "checked_item_ids": ["example-grain"] * 1057},
        {**STATE, "checked_item_ids": "example-grain"},
        {**STATE, "checked_item_ids": [True]},
        {**STATE, "start_date": "2026-02-30"},
        {**STATE, "start_date": "2026-1-1"},
        {**STATE, "start_date": 20260101},
        {**STATE, "private field name": "private content"},
        {"checked_item_ids": []},
        {"start_date": None},
        [],
    ],
)
async def test_invalid_state_preserves_existing_row(application, sessions, payload, caplog):
    async with sessions.begin() as session:
        await import_document(session, sample())
    async with client(application) as http:
        assert (await http.put(URL + "/state", json=STATE, headers=JOB)).status_code == 200
        response = await http.put(URL + "/state", json=payload, headers=JOB)
        assert response.status_code == 422
        assert "private" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert (await http.get(URL)).json()["state"] == STATE
    assert_private_output(caplog.text)


@pytest.mark.parametrize(
    "headers",
    [{}, {**JOB, "Origin": "http://attacker.example"}, {**JOB, "Sec-Fetch-Site": "cross-site"}],
)
async def test_state_write_uses_existing_csrf_protection(application, sessions, headers):
    async with sessions.begin() as session:
        await import_document(session, sample())
    async with client(application) as http:
        response = await http.put(URL + "/state", json=STATE, headers=headers)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert (await http.get(URL)).json()["state"] == EMPTY_STATE


async def test_bounded_stream_and_json_errors_do_not_echo_content(application):
    async with client(application) as http:
        for content, expected in ((b"x" * (128 * 1024 + 1), 413), (b"private content", 422)):
            response = await http.put(URL + "/state", content=content, headers=JOB)
            assert response.status_code == expected
            assert "private content" not in response.text
            assert response.headers["cache-control"] == "no-store"
        response = await http.put(
            URL + "/state", content=b'{"start_date":null,"start_date":null}', headers=JOB
        )
        assert response.status_code == 422

        async def oversized_stream():
            yield b"x" * (128 * 1024)
            yield b"x"

        response = await http.put(URL + "/state", content=oversized_stream(), headers=JOB)
        assert response.status_code == 413
        assert response.headers["cache-control"] == "no-store"


async def test_new_plan_filters_stale_checked_ids_without_destroying_state(application, sessions):
    async with sessions.begin() as session:
        await import_document(session, sample())
    async with client(application) as http:
        await http.put(URL + "/state", json=STATE, headers=JOB)
        async with sessions.begin() as session:
            await import_document(session, {"schema": 1})
        assert (await http.get(URL)).json()["state"] == {**STATE, "checked_item_ids": []}
        rejected = await http.put(URL + "/state", json=STATE, headers=JOB)
        assert rejected.status_code == 422
    async with sessions() as session:
        assert (await session.get(PlanState, 1)).checked_item_ids == STATE["checked_item_ids"]
