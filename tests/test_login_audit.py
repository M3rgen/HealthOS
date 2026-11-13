"""Login audit fields are fixed classifications, never request or credential values."""

import httpx
import pytest
from support.auth import TOKEN, build
from support.log_privacy import MARKER, capture_logs

from app.api import login


@pytest.mark.parametrize(
    "peer,client_class,outcome",
    [
        (peer, client_class, outcome)
        for peer, client_class in [
            (("127.0.0.1", 123), "loopback"),
            (("::1", 123), "loopback"),
            (("::ffff:127.0.0.1", 123), "loopback"),
            (("192.0.2.1", 123), "non_loopback"),
            ((MARKER, 123), "unknown"),
            (None, "unknown"),
        ]
        for outcome in ["success", "failure", "origin", "large", "rate", "disabled"]
        # Tokenless requests are blocked by outer authentication for nonlocal peers.
        if outcome != "disabled" or client_class == "loopback"
    ],
)
async def test_login_emits_one_value_free_event(monkeypatch, peer, client_class, outcome):
    application = build(monkeypatch, token=None if outcome == "disabled" else TOKEN)
    monkeypatch.setattr(login, "FAILED_LOGIN_DELAY_SECONDS", 0)
    if outcome == "rate":
        application.state.login_throttle = login.LoginThrottle(clock=lambda: 100.0)
        while application.state.login_throttle.reserve() is not None:
            pass
    emitted = capture_logs()
    headers = {
        "User-Agent": MARKER,
        "X-Forwarded-For": MARKER,
        "Forwarded": "for=" + MARKER,
        "Authorization": "Bearer " + MARKER,
    }
    if outcome == "origin":
        headers["Origin"] = "https://example.invalid"
    candidate = MARKER if outcome == "failure" else TOKEN
    if outcome == "large":
        candidate += "x" * login.MAX_FORM_BYTES
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application, client=peer),
        base_url="http://healthos.localhost:8000",
    ) as client:
        response = await client.post(
            "/login", data={"token": candidate, "next": "/" + MARKER}, headers=headers
        )
        # Reading the login page is not a submitted attempt.
        await client.get("/login")
    assert (
        response.status_code
        == {
            "success": 303,
            "failure": 401,
            "origin": 403,
            "large": 413,
            "rate": 429,
            "disabled": 303,
        }[outcome]
    )
    [record] = emitted.events("LOGIN_ATTEMPT")
    assert record["message"] == "LOGIN_ATTEMPT"
    assert record["client_class"] == client_class
    assert record["result"] == ("success" if response.status_code == 303 else "failure")
    assert set(record) == {
        "asctime",
        "levelname",
        "name",
        "message",
        "event",
        "client_class",
        "result",
    }
    assert MARKER not in emitted.text() and TOKEN not in emitted.text()
    if response.cookies:
        assert all(cookie not in emitted.text() for cookie in response.cookies.values())
