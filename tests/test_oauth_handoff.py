"""Connecting Google from healthos.localhost never needs a UI login on plain localhost."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from support.auth import TOKEN, build

from app.api.auth import HANDOFF_SECONDS, HandoffTickets
from app.core.auth import COOKIE_NAME as UI_COOKIE
from app.core.oauth_state import COOKIE_NAME as OAUTH_COOKIE
from app.google_health.oauth import OAuthClient

ISOLATED = "http://healthos.localhost:8000"
CALLBACK_ORIGIN = "http://localhost:8000"


@pytest.fixture
def application(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("GOOGLE_REDIRECT_URI", CALLBACK_ORIGIN + "/auth/google/callback")
    app = build(
        monkeypatch,
        google_client_id="synthetic-client",
        google_client_secret="synthetic-secret",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )
    oauth = OAuthClient(app.state.settings, http=SimpleNamespace())
    app.state.runtime.oauth = SimpleNamespace(authorization_url=oauth.authorization_url)
    return app


def client(application, base):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=base)


@asynccontextmanager
async def logged_in(application):
    async with client(application, ISOLATED) as http:
        response = await http.post("/login", data={"token": TOKEN, "next": "/"})
        assert response.status_code == 303
        yield http


async def test_start_on_the_isolated_host_reaches_google_without_a_localhost_login(application):
    async with logged_in(application) as ui:
        moved = await ui.get("/auth/google/start", headers={"Accept": "text/html"})
    assert moved.status_code == 303
    location = urlsplit(moved.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == CALLBACK_ORIGIN
    assert location.path == "/auth/google/handoff"
    assert "set-cookie" not in moved.headers
    assert not application.state.oauth_states._pending  # nothing minted off the callback origin
    async with client(application, CALLBACK_ORIGIN) as callback_origin:
        started = await callback_origin.get(moved.headers["location"])
        assert started.status_code == 307
        google = urlsplit(started.headers["location"])
        assert google.netloc == "accounts.google.com"
        assert parse_qs(google.query)["state"]
        cookies = {cookie.name for cookie in callback_origin.cookies.jar}
    # Only the path-scoped OAuth binding lands on localhost, never a UI session.
    assert cookies == {OAUTH_COOKIE}
    assert UI_COOKIE not in started.headers["set-cookie"]
    assert "path=/auth/google" in started.headers["set-cookie"].lower()


async def test_a_ticket_is_single_use(application):
    async with logged_in(application) as ui:
        moved = await ui.get("/auth/google/start")
    async with client(application, CALLBACK_ORIGIN) as callback_origin:
        assert (await callback_origin.get(moved.headers["location"])).status_code == 307
        again = await callback_origin.get(moved.headers["location"])
    assert again.status_code == 400
    assert "yeniden başlatın" in again.json()["detail"]


@pytest.mark.parametrize("ticket", ["", "forged-ticket", "x" * 43])
async def test_unknown_tickets_are_refused(application, ticket):
    async with client(application, CALLBACK_ORIGIN) as callback_origin:
        response = await callback_origin.get("/auth/google/handoff", params={"ticket": ticket})
    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    assert not application.state.oauth_states._pending


async def test_handoff_is_spent_only_on_the_callback_origin(application):
    async with logged_in(application) as ui:
        moved = await ui.get("/auth/google/start")
        ticket = parse_qs(urlsplit(moved.headers["location"]).query)["ticket"][0]
        elsewhere = await ui.get("/auth/google/handoff", params={"ticket": ticket})
    assert elsewhere.status_code == 400
    async with client(application, CALLBACK_ORIGIN) as callback_origin:
        # Not consumed by the wrong origin: the user's own redirect still works.
        assert (await callback_origin.get(moved.headers["location"])).status_code == 307


async def test_start_still_needs_the_ui_login(application):
    async with client(application, ISOLATED) as anonymous:
        response = await anonymous.get("/auth/google/start")
    assert response.status_code == 401


def test_tickets_expire_and_the_store_stays_bounded():
    now = [100.0]
    tickets = HandoffTickets(clock=lambda: now[0])
    first = tickets.issue()
    now[0] += HANDOFF_SECONDS + 1
    assert not tickets.spend(first)
    issued = [tickets.issue() for _ in range(HandoffTickets.MAX_PENDING + 4)]
    assert len(tickets._expiry) == HandoffTickets.MAX_PENDING
    assert not tickets.spend(issued[0])  # the oldest made room
    assert tickets.spend(issued[-1])
    assert all(len(key) == 64 for key in tickets._expiry)  # only hashes are kept
