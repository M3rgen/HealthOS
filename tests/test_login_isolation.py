"""Default logins keep UI sessions off shared loopback cookie domains."""

from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from support.auth import TOKEN, build
from support.log_privacy import capture_logs

from app.core.config import Settings


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("port", [8000, 18080])
@pytest.mark.parametrize(
    "target,safe", [("/page-probe?x=1&y=2", "/page-probe?x=1&y=2"), ("//evil.example", "/")]
)
async def test_shared_host_login_moves_without_a_cookie(monkeypatch, host, port, target, safe):
    from urllib.parse import urlencode

    application = build(monkeypatch)
    emitted = capture_logs()
    base = f"http://{host}:{port}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url=base
    ) as client:
        response = await client.post(
            "/login", data={"token": TOKEN, "next": target}, headers={"Origin": base}
        )
        assert response.status_code == 409
        assert "set-cookie" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["location"] == (
            f"http://healthos.localhost:{port}/login?{urlencode({'next': safe})}"
        )
        assert TOKEN not in response.headers["location"]
        assert 'href="http://healthos.localhost:' in response.text
        assert [record["result"] for record in emitted.events("LOGIN_ATTEMPT")] == ["failure"]
        assert (await client.get("/api/probe")).status_code == 401
        isolated = await client.get(response.headers["location"])
        assert isolated.status_code == 200 and 'name="token"' in isolated.text


@pytest.mark.parametrize(
    "base,public_url",
    [
        ("http://healthos.localhost:8000", None),
        ("https://localhost:8000", None),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://localhost:8000"),
        ("http://localhost:8000", "https://ui.example.org"),
    ],
)
async def test_isolated_secure_and_explicit_interface_logins_keep_cookies(
    monkeypatch, base, public_url
):
    application = build(monkeypatch, app_public_url=public_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url=base
    ) as client:
        response = await client.post("/login", data={"token": TOKEN, "next": "/page-probe"})
    assert response.status_code == 303 and response.headers["location"] == "/page-probe"
    assert "healthos_ui" in response.cookies


def test_google_callback_defaults_to_the_isolated_host(monkeypatch):
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    uri = Settings(_env_file=None).google_redirect_uri
    assert urlsplit(uri).hostname == "healthos.localhost"
    assert f"GOOGLE_REDIRECT_URI={uri}\n" in Path(".env.example").read_text()
