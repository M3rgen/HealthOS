"""Session expiry must be discovered by the next chat message itself."""

import asyncio
import json

from test_codex_chat import SiteSocket, SyntheticBridge

from app.api import codex
from app.core.auth import COOKIE_NAME, SessionStore


async def test_chat_route_rechecks_expired_cookie_before_starting_another_turn(monkeypatch):
    now = 0
    sessions = SessionStore(ttl_seconds=60, clock=lambda: now)
    session = sessions.issue()
    site, bridge = SiteSocket(), SyntheticBridge()
    site.app.state.settings.app_ui_token = "synthetic-ui-token"
    site.app.state.ui_sessions = sessions
    site.scope["headers"].append((b"cookie", f"{COOKIE_NAME}={session}".encode()))
    monkeypatch.setattr(codex, "connect", lambda *args, **kwargs: bridge)

    try:
        assert (await site.start())["type"] == "websocket.accept"
        assert (await site.message())["type"] == "ready"
        bridge.stall = True
        await site.incoming.put(
            {"type": "websocket.receive", "text": json.dumps({"text": "İlk soru"})}
        )
        while (await site.message())["type"] != "working":
            pass
        turns = [request for request in bridge.requests if request["method"] == "turn/start"]
        assert len(turns) == 1
        await bridge.responses.put(
            json.dumps({"id": turns[0]["id"], "result": {"turn": {"id": "first-turn"}}})
        )
        await bridge.responses.put(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "synthetic-thread",
                        "turn": {"id": "first-turn", "status": "completed"},
                    },
                }
            )
        )
        while (message := await site.message())["type"] != "done":
            pass
        assert message["status"] == "completed"

        # Only advance time: valid(), HTTP requests or revocation would notify
        # watchers first and mask a missing per-message authorization callback.
        now = 60
        await site.incoming.put(
            {"type": "websocket.receive", "text": json.dumps({"text": "İkinci soru"})}
        )
        frame = await site.frame()
        assert frame["type"] == "websocket.close" and frame["code"] == 1008
        await asyncio.wait_for(site.task, 30)
        assert bridge.closed
        assert [
            request for request in bridge.requests if request["method"] == "turn/start"
        ] == turns
    finally:
        if not site.task.done():
            await site.leave()
