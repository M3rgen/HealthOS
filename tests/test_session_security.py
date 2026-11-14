from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from test_auth import BASE, TOKEN, build, client

from app.core.config import Settings
from app.services.synchronization.pipeline_health import PipelineHealthService


@pytest.mark.parametrize(
    "base,overrides,forwarded,secure",
    [
        (BASE, {}, [], False),
        (BASE.replace("http:", "https:"), {}, [], True),
        (BASE, {"app_public_url": "HTTPS://localhost:8000"}, [], True),
        (BASE, {}, [("X-Forwarded-Proto", "https")], False),
        (BASE, {"app_trust_proxy_headers": True}, [("X-Forwarded-Proto", "https")], True),
        (BASE, {"app_trust_proxy_headers": True}, [("X-Forwarded-Proto", "https,http")], False),
        (
            BASE,
            {"app_trust_proxy_headers": True},
            [("X-Forwarded-Proto", "https"), ("X-Forwarded-Proto", "http")],
            False,
        ),
        (
            BASE.replace("http:", "https:"),
            {"app_trust_proxy_headers": True},
            [("X-Forwarded-Proto", "http")],
            True,
        ),
    ],
)
async def test_session_cookies_share_transport_policy(
    monkeypatch, base, overrides, forwarded, secure
):
    application = build(
        monkeypatch,
        google_client_id="synthetic-client",
        google_client_secret="synthetic-secret",
        google_token_encryption_key=Fernet.generate_key().decode(),
        # OAuth state is minted only on the callback origin (#282); keep it this client's.
        google_redirect_uri=f"{base}/auth/google/callback",
        **({"app_public_url": BASE} | overrides),
    )
    application.state.runtime.oauth = SimpleNamespace(
        authorization_url=lambda *_, **__: "https://accounts.example/consent"
    )
    origin = (
        base.replace("http:", "https:")
        if secure and forwarded == [("X-Forwarded-Proto", "https")]
        else base
    )
    headers = [*forwarded, ("Origin", origin)]
    async with client(application, base) as http:
        login = await http.post("/login", data={"token": TOKEN}, headers=headers)
        assert login.status_code == 303
        session = login.cookies["healthos_ui"]
        oauth = await http.get(
            "/auth/google/start", headers=[*forwarded, ("Authorization", f"Bearer {TOKEN}")]
        )
        assert oauth.status_code == 307
        logout = await http.post(
            "/logout", headers=[*headers, ("Cookie", f"healthos_ui={session}")]
        )
        assert logout.status_code == 303
        assert not application.state.ui_sessions.valid(session)
    for response in (login, oauth, logout):
        attributes = {part.strip().lower() for part in response.headers["set-cookie"].split(";")}
        assert ("secure" in attributes) is secure
        assert "httponly" in attributes
    assert "samesite=strict" in login.headers["set-cookie"].lower()
    assert "samesite=lax" in oauth.headers["set-cookie"].lower()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_tokenless_loopback_settings_remain_valid(host):
    assert Settings(_env_file=None, app_host=host, app_ui_token=None).app_ui_token is None


@pytest.mark.parametrize(
    "host,token",
    [
        ("0.0.0.0", None),
        ("0.0.0.0", ""),
        ("0.0.0.0", "  "),
        ("::", None),
        ("remote.example", None),
        ("127.0.0.2", None),
        ("[::1]", None),
    ],
)
def test_tokenless_nonloopback_settings_fail(host, token, monkeypatch):
    monkeypatch.setenv("APP_HOST", host)
    monkeypatch.setenv("APP_UI_TOKEN", token or "")
    with pytest.raises(ValidationError, match="APP_HOST.*APP_UI_TOKEN"):
        Settings(_env_file=None)


@pytest.mark.parametrize("token", [None, TOKEN])
@pytest.mark.parametrize("database_available", [False, True])
async def test_health_reports_login_state_even_when_database_is_unavailable(
    monkeypatch, token, database_available, sessions
):
    application = build(monkeypatch, token=token)
    runtime = application.state.runtime
    runtime.scheduler_needed = lambda _: False
    runtime.scheduler_running = False
    snapshot = await PipelineHealthService(application.state.settings, sessions, set()).inspect()
    inspect = AsyncMock(return_value=snapshot)
    if not database_available:
        inspect.side_effect = OSError("synthetic database failure")
    runtime.pipeline_health = SimpleNamespace(inspect=inspect, sessions=sessions)
    async with client(application) as http:
        response = await http.get(
            "/health", headers={"Authorization": f"Bearer {TOKEN}"} if token else {}
        )
    assert response.status_code == (200 if database_available else 503)
    assert response.json()["ui_login_disabled"] is (token is None)


def test_launchers_leave_forwarded_headers_to_explicit_app_policy():
    root = Path(__file__).resolve().parents[1]
    for name in (
        "Dockerfile",
        "Makefile",
        "docs/DEVELOPMENT.md",
        "docs/reference/setup.md",
        "docs/MIGRATIONS.md",
    ):
        launch = next(
            line
            for line in (root / name).read_text().splitlines()
            if "uvicorn app.main:app " in line
        )
        assert "--no-proxy-headers" in launch
