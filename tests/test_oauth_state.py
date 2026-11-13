import asyncio
import base64
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.api import auth
from app.core.auth import SessionStore, UITokenMiddleware
from app.core.config import Settings
from app.core.host_validation import HostValidationMiddleware
from app.core.oauth_state import (
    COOKIE_NAME,
    COOKIE_PATH,
    STATE_TTL,
    OAuthStateError,
    OAuthStateStore,
    OAuthStateUnavailable,
)
from app.google_health.oauth import OAuthClient


@pytest.fixture
def application():
    @asynccontextmanager
    async def updating():
        yield

    app = FastAPI()
    app.include_router(auth.router)
    app.state.settings = Settings(
        _env_file=None,
        google_redirect_uri="http://localhost:8000/auth/google/callback",
        google_client_id="synthetic-client",
        google_client_secret="synthetic-secret",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )
    app.state.oauth_states = OAuthStateStore()
    app.state.runtime = SimpleNamespace(
        oauth=SimpleNamespace(
            authorization_url=OAuthClient(
                app.state.settings, http=SimpleNamespace()
            ).authorization_url,
            exchange_code=AsyncMock(
                return_value={
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                }
            ),
        ),
        google=SimpleNamespace(
            get_identity=AsyncMock(return_value={"healthUserId": "synthetic-account"})
        ),
        credentials=SimpleNamespace(check_account=AsyncMock(), save_connection=AsyncMock()),
        tokens=SimpleNamespace(credential_update=updating),
    )
    app.add_middleware(UITokenMiddleware, token="synthetic-ui-token", sessions=SessionStore())
    app.add_middleware(HostValidationMiddleware)
    return app


async def begin(client):
    response = await client.get(
        "/auth/google/start", headers={"Authorization": "Bearer synthetic-ui-token"}
    )
    assert response.status_code == 307
    return response, parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


@pytest.mark.parametrize(
    "origin", ["http://healthos.localhost:8000", "http://localhost:8001", "https://localhost:8000"]
)
async def test_start_moves_to_callback_origin_before_issuing_state(application, origin):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url=origin
    ) as client:
        moved = await client.get(
            "/auth/google/start", headers={"Authorization": "Bearer synthetic-ui-token"}
        )
        target = "http://localhost:8000/auth/google/handoff?ticket="
        assert moved.status_code == 303 and moved.headers["location"].startswith(target)
        assert moved.headers["cache-control"] == "no-store"
        assert "set-cookie" not in moved.headers and not application.state.oauth_states._pending
        # No UI credential on the callback origin: the single-use ticket carries the start.
        started = await client.get(moved.headers["location"])
        assert started.status_code == 307
        assert next(iter(client.cookies.jar)).domain == "localhost.local"
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        response = await client.get(
            application.state.settings.google_redirect_uri,
            params={"code": "synthetic-code", "state": state},
        )
    assert response.status_code == 200 and response.json()["status"] == "connected"
    application.state.runtime.oauth.exchange_code.assert_awaited_once()
    assert application.state.runtime.oauth.exchange_code.call_args.args == ("synthetic-code",)


@pytest.mark.parametrize("host", ["attacker.example", "healthos.localhost:9000"])
async def test_start_target_ignores_host_forwarded_headers_and_query(application, host):
    application.state.settings.google_redirect_uri = (
        "https://localhost:8443/auth/google/callback?configured=query#fragment"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost:8000"
    ) as client:
        response = await client.get(
            "/auth/google/start?next=https://attacker.example/other",
            headers={
                "Authorization": "Bearer synthetic-ui-token",
                "Host": host,
                "X-Forwarded-Host": "attacker.example",
                "X-Forwarded-Proto": "https",
            },
        )
    assert not application.state.oauth_states._pending and "set-cookie" not in response.headers
    if host == "attacker.example":
        assert response.status_code == 400 and "location" not in response.headers
    else:
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "https://localhost:8443/auth/google/handoff?ticket="
        )


async def test_normal_redirect_is_bound_to_cookie_and_only_one_callback_wins(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost:8000"
    ) as client:
        response, state = await begin(client)
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert f"path={COOKIE_PATH}" in cookie and "max-age" not in cookie
        assert response.headers["cache-control"] == "no-store"
        stored = repr(application.state.oauth_states._pending)
        assert client.cookies[COOKIE_NAME] not in stored
        assert hashlib.sha256(client.cookies[COOKIE_NAME].encode()).hexdigest() in stored
        params = {"code": "synthetic-code", "state": state}
        # Google's cross-site GET callback carries the Lax cookie, without the UI bearer.
        responses = await asyncio.gather(
            *[
                client.get(
                    "/auth/google/callback", params=params, headers={"Sec-Fetch-Site": "cross-site"}
                )
                for _ in range(2)
            ]
        )
    assert sorted(response.status_code for response in responses) == [200, 400]
    application.state.runtime.oauth.exchange_code.assert_awaited_once()
    assert application.state.runtime.oauth.exchange_code.call_args.args == ("synthetic-code",)
    application.state.runtime.credentials.save_connection.assert_awaited_once()


async def test_other_or_missing_cookie_and_tampering_do_not_consume_valid_state(application):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://localhost:8000"
    ) as owner:
        _, state = await begin(owner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://localhost:8000"
        ) as other:
            _, other_state = await begin(other)
            assert other.cookies[COOKIE_NAME] != owner.cookies[COOKIE_NAME]
            for headers in ({}, {"Cookie": ""}):
                rejected = await other.get(
                    "/auth/google/callback", params={"code": "x", "state": state}, headers=headers
                )
                assert rejected.status_code == 400
                if headers:
                    assert "Bağlantıyı aynı adresten yeniden başlatın" in rejected.json()["detail"]
        payload, signature = state.rsplit(".", 1)
        tampered = f"{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
        assert (
            await owner.get("/auth/google/callback", params={"code": "x", "state": tampered})
        ).status_code == 400
        application.state.runtime.oauth.exchange_code.assert_not_awaited()
        assert (
            await owner.get("/auth/google/callback", params={"code": "x", "state": state})
        ).status_code == 200
        assert other_state != state


async def test_state_remains_consumed_when_exchange_fails(application):
    exchange = application.state.runtime.oauth.exchange_code
    exchange.side_effect = RuntimeError("Synthetic provider failure")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application, raise_app_exceptions=False),
        base_url="http://localhost:8000",
    ) as client:
        _, state = await begin(client)
        params = {"code": "x", "state": state}
        assert (await client.get("/auth/google/callback", params=params)).status_code == 500
        assert (await client.get("/auth/google/callback", params=params)).status_code == 400
    exchange.assert_awaited_once()


@pytest.mark.parametrize("origin", ["http://localhost:8000", "http://healthos.localhost:8000"])
async def test_pkce_is_s256_unique_and_bound_to_each_single_use_state(application, origin):
    settings = application.state.settings
    settings.google_redirect_uri = origin + "/auth/google/callback"
    challenges = {}
    exchanged = []

    def exchange(request):
        form = parse_qs(request.content.decode())
        verifier = form["code_verifier"][0]
        assert len(verifier) == 43
        assert set(verifier) <= set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        code = form["code"][0]
        assert challenge == challenges[code]
        assert form["redirect_uri"] == [settings.google_redirect_uri]
        assert form["grant_type"] == ["authorization_code"]
        exchanged.append(code)
        return httpx.Response(
            200, json={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as provider:
        application.state.runtime.oauth = OAuthClient(settings, http=provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url=origin
        ) as client:
            states = {}
            for code in ("first", "second"):
                response, states[code] = await begin(client)
                query = parse_qs(urlsplit(response.headers["location"]).query)
                assert query["code_challenge_method"] == ["S256"]
                assert "code_verifier" not in query
                challenges[code] = query["code_challenge"][0]
                verifier = application.state.oauth_states._pending[states[code].split(".")[1]][2]
                assert verifier not in response.headers["location"]
                assert verifier not in response.headers["set-cookie"]
            assert challenges["first"] != challenges["second"]
            for code in ("second", "first"):
                params = {"code": code, "state": states[code]}
                assert (await client.get("/auth/google/callback", params=params)).status_code == 200
                assert (await client.get("/auth/google/callback", params=params)).status_code == 400
    assert exchanged == ["second", "first"]
    assert not application.state.oauth_states._pending


def test_expiry_capacity_cleanup_and_restart_fail_closed():
    now = 0
    store = OAuthStateStore(clock=lambda: now)
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    state = store.issue(settings, "synthetic-cookie", code_verifier="synthetic-verifier")
    for _ in range(store.MAX_PENDING - 1):
        store.issue(settings, "synthetic-cookie", code_verifier="synthetic-verifier")
    with pytest.raises(OAuthStateUnavailable) as full:
        store.issue(settings, "synthetic-cookie", code_verifier="synthetic-verifier")
    assert "Too many pending" in str(full.value)
    now = STATE_TTL
    with pytest.raises(OAuthStateError) as expired:
        store.consume(settings, state, "synthetic-cookie")
    assert "Invalid or expired" in str(expired.value)
    state = store.issue(settings, "synthetic-cookie", code_verifier="synthetic-verifier")
    assert len(store._pending) == 1
    with pytest.raises(OAuthStateError):
        OAuthStateStore().consume(settings, state, "synthetic-cookie")
    store.consume(settings, state, "synthetic-cookie")


@pytest.mark.parametrize("legacy_key", ["", "current", "old"])
def test_pre_hkdf_state_is_rejected_during_transition(legacy_key):
    root, old = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None, google_token_encryption_key=root, google_token_encryption_keys_old=[old]
    )
    secret = {"": "", "current": root, "old": old}[legacy_key]
    payload = f"{int(time.time())}.synthetic-nonce"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    legacy = f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"
    with pytest.raises(OAuthStateError) as rejected:
        OAuthStateStore().consume(settings, legacy, "synthetic-cookie")
    assert "Invalid or expired" in str(rejected.value)


@pytest.mark.parametrize("key", [None, "", "malformed-key"])
def test_invalid_key_cannot_mint_or_validate_state(key):
    settings = Settings(_env_file=None, google_token_encryption_key=key)
    store = OAuthStateStore()
    with pytest.raises(OAuthStateUnavailable) as unavailable:
        store.issue(settings, "synthetic-cookie", code_verifier="synthetic-verifier")
    assert "encryption key" in str(unavailable.value) and not store._pending
    with pytest.raises(OAuthStateError) as rejected:
        store.consume(settings, "v1.nonce.binding.signature", "synthetic-cookie")
    assert "Invalid or expired" in str(rejected.value)
