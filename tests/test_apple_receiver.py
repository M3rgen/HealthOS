"""POST /webhooks/apple-health: the Health Auto Export receiver (synthetic bodies only)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from apple_fixtures import hae_body, hae_heart_rate, hae_steps
from sqlalchemy import func, select

from app import main
from app.core.apple_paths import APPLE_RECEIVER_PATH
from app.core.config import Settings
from app.models.apple import AppleImport, ApplePayload, AppleRawRecord

TOKEN = "synthetic-receiver-token-" + "x" * 24
UI_TOKEN = "u" * 20 + "-test-ui-token-0123456789"


def build(monkeypatch, sessions, tmp_path, *, token: str | None = TOKEN, **overrides):
    token_file = tmp_path / "apple-token"
    if token is not None:
        token_file.write_text(token + "\n")
    values = {
        "app_env": "test",
        "app_ui_token": "",
        "codex_bridge_token": None,
        "apple_receiver_enabled": True,
        "apple_receiver_token_file": token_file,
        "apple_receiver_endpoint_uri": "https://apple.example/webhooks/apple-health",
        **overrides,
    }
    settings = Settings(_env_file=None, **values)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main,
        "build_runtime",
        lambda *_: SimpleNamespace(webhook_status=SimpleNamespace(record=AsyncMock())),
    )
    application = main.create_app()
    application.state.apple_sessions = sessions
    return application


def client(application, base="http://localhost"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=base)


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def count(sessions, model) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


BODY = hae_body(metrics=[hae_steps(), hae_heart_rate()])


async def test_disabled_receiver_is_404_before_anything_else(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path, apple_receiver_enabled=False)
    async with client(application) as http:
        response = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
    assert response.status_code == 404
    assert await count(sessions, ApplePayload) == 0


async def test_missing_or_short_token_file_is_503(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path, token=None)
    async with client(application) as http:
        missing = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
    assert missing.status_code == 503
    assert missing.json()["detail"] == "receiver token not configured"
    short = build(monkeypatch, sessions, tmp_path, token="too-short")
    async with client(short) as http:
        response = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer("too-short"))
    assert response.status_code == 503
    assert await count(sessions, ApplePayload) == 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-" + "y" * 30},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
    ],
)
async def test_missing_or_wrong_token_is_401(monkeypatch, sessions, tmp_path, headers):
    application = build(monkeypatch, sessions, tmp_path)
    async with client(application) as http:
        response = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=headers)
    assert response.status_code == 401
    assert await count(sessions, ApplePayload) == 0


async def test_duplicate_authorization_headers_are_refused(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path)
    async with client(application) as http:
        response = await http.post(
            APPLE_RECEIVER_PATH,
            content=BODY,
            headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
        )
    assert response.status_code == 401


async def test_authorized_body_is_archived_normalized_and_kept_once(
    monkeypatch, sessions, tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    application = build(monkeypatch, sessions, tmp_path)
    headers = {**bearer(), "automation-name": "Synthetic automation", "session-id": "s-1"}
    async with client(application) as http:
        first = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=headers)
        again = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=headers)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["status"] == "ok"
    assert result["records"] == 2 and result["new"] == 2
    assert result["rows"] >= 1
    assert set(result) == {"status", "records", "new", "rows", "skipped", "invalid"}
    assert first.headers["cache-control"] == "no-store"
    assert again.status_code == 200
    assert again.json() == {
        "status": "duplicate",
        "records": 0,
        "new": 0,
        "rows": 0,
        "skipped": 0,
        "invalid": 0,
    }
    assert await count(sessions, ApplePayload) == 1
    assert await count(sessions, AppleRawRecord) == 2
    async with sessions() as session:
        payload = (await session.scalars(select(ApplePayload))).one()
        imports = (await session.scalars(select(AppleImport))).all()
    assert payload.body == BODY
    assert payload.headers["automation-name"] == "Synthetic automation"
    assert not any(name.lower() == "authorization" for name in payload.headers)
    assert [(item.channel, item.origin, item.status) for item in imports] == [
        ("hae", "receiver", "done")
    ]
    assert imports[0].records_new == 2
    # Neither the token nor the body reaches the log.
    assert TOKEN not in caplog.text
    assert "step_count" not in caplog.text


async def test_changed_day_total_adds_a_version(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path)
    async with client(application) as http:
        first = await http.post(
            APPLE_RECEIVER_PATH, content=hae_body(metrics=[hae_steps(1000)]), headers=bearer()
        )
        later = await http.post(
            APPLE_RECEIVER_PATH, content=hae_body(metrics=[hae_steps(2500)]), headers=bearer()
        )
    assert first.json()["new"] == later.json()["new"] == 1
    assert await count(sessions, ApplePayload) == 2
    assert await count(sessions, AppleRawRecord) == 2


async def test_body_over_the_limit_is_413(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path, apple_receiver_max_body_bytes=1024 * 1024)
    big = hae_body(metrics=[hae_steps()], padding=["x" * 1024] * 1100)
    async with client(application) as http:
        declared = await http.post(APPLE_RECEIVER_PATH, content=big, headers=bearer())

        async def chunks():
            for offset in range(0, len(big), 64 * 1024):
                yield big[offset : offset + 64 * 1024]

        streamed = await http.post(APPLE_RECEIVER_PATH, content=chunks(), headers=bearer())
    assert declared.status_code == streamed.status_code == 413
    assert await count(sessions, ApplePayload) == 0


@pytest.mark.parametrize("body", [b"{not json", b'{"data": []}', b"[1, 2]", b"\xff\xfe"])
async def test_invalid_body_is_400_and_kept_as_evidence(monkeypatch, sessions, tmp_path, body):
    application = build(monkeypatch, sessions, tmp_path)
    async with client(application) as http:
        response = await http.post(APPLE_RECEIVER_PATH, content=body, headers=bearer())
        again = await http.post(APPLE_RECEIVER_PATH, content=body, headers=bearer())
    assert response.status_code == 400
    assert again.json()["status"] == "duplicate"
    async with sessions() as session:
        payload = (await session.scalars(select(ApplePayload))).one()
        imports = (await session.scalars(select(AppleImport))).all()
    assert payload.body == body
    assert [(item.status, item.origin) for item in imports] == [("failed", "receiver")]
    assert imports[0].error


async def test_unauthorized_flood_cannot_starve_the_phone(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path, apple_receiver_requests_per_minute=2)
    async with client(application) as http:
        refused = [
            (await http.post(APPLE_RECEIVER_PATH, content=BODY)).status_code for _ in range(3)
        ]
        allowed = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
    assert refused == [401, 401, 429]
    assert allowed.status_code == 200
    assert application.state.apple_limits.unauthorized.rejected == 1


async def test_authorized_budget_is_enforced_too(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path, apple_receiver_requests_per_minute=1)
    async with client(application) as http:
        first = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
        second = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
    assert first.status_code == 200
    assert second.status_code == 429
    assert "retry-after" in second.headers


async def test_public_host_reaches_only_the_receiver(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path)
    async with client(application, "https://apple.example") as http:
        hook = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
        status = await http.get("/api/apple/status")
        upload = await http.post(
            "/api/apple/import", content=b"x", headers={"X-HealthOS-Request": "1"}
        )
        wrong_method = await http.get(APPLE_RECEIVER_PATH)
        google = await http.post("/webhooks/google-health", json={"type": "verification"})
    assert hook.status_code == 200
    assert status.status_code == upload.status_code == wrong_method.status_code == 400
    assert google.status_code == 400


async def test_receiver_skips_the_ui_login_but_keeps_its_own_token(monkeypatch, sessions, tmp_path):
    application = build(monkeypatch, sessions, tmp_path, app_ui_token=UI_TOKEN)
    async with client(application) as http:
        hook = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer())
        ui_token_only = await http.post(APPLE_RECEIVER_PATH, content=BODY, headers=bearer(UI_TOKEN))
        anonymous = await http.post(APPLE_RECEIVER_PATH, content=BODY)
        status = await http.get("/api/apple/status")
        status_with_receiver_token = await http.get("/api/apple/status", headers=bearer())
    assert hook.status_code == 200
    # The UI token is not the receiver's token, and the receiver answers for itself.
    assert ui_token_only.status_code == anonymous.status_code == 401
    assert anonymous.json()["detail"] == "Invalid receiver authorization"
    # Every other private route still needs the UI token; the receiver token opens nothing.
    assert status.status_code == status_with_receiver_token.status_code == 401
    assert status.json()["detail"] == "auth_required"
