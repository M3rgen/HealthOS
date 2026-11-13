"""A live site socket must stop admitting turns when its login stops being valid."""

import asyncio
import json

import httpx
import pytest
from test_codex_chat import SiteSocket, SyntheticBridge

from app.api import codex, login
from app.core.auth import COOKIE_NAME, SessionStore, UITokenMiddleware


@pytest.mark.parametrize("invalidation", ["logout", "expiry", "eviction"])
async def test_invalid_session_closes_live_socket_before_next_model_turn(monkeypatch, invalidation):
    now = 0
    sessions = SessionStore(ttl_seconds=60, clock=lambda: now)
    sessions.MAX_SESSIONS = 1
    session = sessions.issue()
    site, bridge = SiteSocket(), SyntheticBridge()
    site.app.state.settings.app_ui_token = "synthetic-ui-token"
    site.app.state.settings.app_trust_proxy_headers = False
    site.app.state.settings.app_public_url = None
    site.app.state.ui_sessions = sessions
    site.app.include_router(login.router)
    site.app.add_middleware(UITokenMiddleware, token="synthetic-ui-token", sessions=sessions)
    site.scope["headers"].append((b"cookie", f"{COOKIE_NAME}={session}".encode()))
    monkeypatch.setattr(codex, "connect", lambda *args, **kwargs: bridge)
    assert (await site.start())["type"] == "websocket.accept"
    assert (await site.message())["type"] == "ready"

    try:
        # A valid session delivers a real turn through the production ASGI relay.
        # Finish it before invalidation so the next message would start a new turn.
        bridge.stall = True
        await site.incoming.put(
            {"type": "websocket.receive", "text": json.dumps({"text": "İlk soru"})}
        )
        while (await site.message())["type"] != "working":
            pass
        turns = [r for r in bridge.requests if r["method"] == "turn/start"]
        assert len(turns) == 1
        await bridge.responses.put(
            json.dumps({"id": turns[0]["id"], "result": {"turn": {"id": "t"}}})
        )
        await bridge.responses.put(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "synthetic-thread",
                        "turn": {"id": "t", "status": "completed"},
                    },
                }
            )
        )
        while (await site.message())["type"] != "done":
            pass
        if invalidation == "expiry":
            now = 60
        elif invalidation == "eviction":
            sessions.issue()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(site.app), base_url="http://localhost:8000"
        ) as http:
            if invalidation == "logout":
                response = await http.post(
                    "/logout",
                    headers={
                        "Cookie": f"{COOKIE_NAME}={session}",
                        "Origin": "http://localhost:8000",
                    },
                )
                assert response.status_code == 303
            response = await http.get("/private", headers={"Cookie": f"{COOKIE_NAME}={session}"})
        assert response.status_code == 401
        await site.incoming.put(
            {"type": "websocket.receive", "text": json.dumps({"text": "İkinci soru"})}
        )
        frame = await site.frame()
        assert frame["type"] == "websocket.close" and frame["code"] == 1008
        await asyncio.wait_for(site.task, 2)
        assert bridge.closed
        assert [r for r in bridge.requests if r["method"] == "turn/start"] == turns
    finally:
        if not site.task.done():
            await site.leave()


@pytest.mark.parametrize("bearer", [False, True])
async def test_new_turn_preserves_bearer_and_development_tokenless_access(monkeypatch, bearer):
    site, bridge = SiteSocket(), SyntheticBridge()
    if bearer:
        site.app.state.settings.app_ui_token = "synthetic-ui-token"
        site.scope["headers"].append((b"authorization", b"Bearer synthetic-ui-token"))
    monkeypatch.setattr(codex, "connect", lambda *args, **kwargs: bridge)
    assert (await site.start())["type"] == "websocket.accept"
    assert (await site.message())["type"] == "ready"
    try:
        bridge.stall = True
        await site.incoming.put({"type": "websocket.receive", "text": json.dumps({"text": "Soru"})})
        while (await site.message())["type"] != "working":
            pass
        assert len([r for r in bridge.requests if r["method"] == "turn/start"]) == 1
    finally:
        await site.leave()
    assert bridge.closed
