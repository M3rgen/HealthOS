"""Security behaviours of round 2: secrets, login limit, session lifetime, logout, no-store."""

import asyncio
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from support.auth import BASE, TOKEN, build
from support.chat import RPC, Browser

from app.api import health as health_api
from app.api import login as login_routes
from app.core import auth
from app.core.config import Settings, strong_secret
from app.core.security_headers import NO_STORE_PREFIXES
from app.db import role_check
from app.services.umay import chat_relay


@pytest.fixture
def application(monkeypatch):
    # Explicit legacy interface: the shared-host session policy remains supported.
    return build(monkeypatch, app_public_url=BASE)


def client(application, base=BASE):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=base)


# Strength check


@pytest.mark.parametrize(
    "value",
    [
        "password" * 4,  # the finding: passes length, repeats a word
        "Bearer aaaaaaaab",  # the finding: 16 with its scheme, 9 and one character without
        "Bearer " + "a" * 40,
        "Basic " + "x" * 32,
        "T" * 31 + "x",
        "abcd" * 8,
        "hunter2hunter3hunter",
        "Bearer hunter2hunter3hunter",
        "Hunter2HUNTER3hunter",
        "hunter2hunter3hunter4",
        "hunter-hunter_hunter!",
        "randomword1randomword2",
        "hunter123hunter234hunter345hunter456",
        "hunter2hunter3hunter-2026",
        "hunter12345hunter23456hunter34567",
        "hunter2hunter3hunt",
        "abcdefghijklmnopqrstuvwx",
        "9876543210987654",
        "1111111111111112",
        "short-123456789",
        "",
    ],
)
def test_weak_secrets_are_refused(value):
    assert strong_secret(value) is False


@pytest.mark.parametrize(
    "generate",
    [
        pytest.param(lambda: "Bearer " + secrets.token_urlsafe(32), id="bearer-urlsafe"),
        pytest.param(lambda: secrets.token_hex(32), id="hex"),
        pytest.param(lambda: secrets.token_urlsafe(48), id="urlsafe"),
        pytest.param(lambda: "synthetic-webhook-secret-0123456789", id="hand-written"),
    ],
)
def test_generated_secrets_are_accepted(generate):
    assert strong_secret(generate()) is True


def test_every_generator_in_the_guides_produces_an_accepted_secret():
    for _ in range(300):
        assert strong_secret(secrets.token_urlsafe(32)) and strong_secret(secrets.token_hex(32))
        assert strong_secret("Bearer " + secrets.token_urlsafe(32))


def test_a_refused_ui_token_is_named_but_its_value_is_not_printed():
    weak = "password" * 4
    with pytest.raises(ValueError) as failure:
        Settings(
            _env_file=None,
            app_env="production",
            healthos_sources="none",
            database_url="postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health",
            app_ui_token=weak,
        )
    assert "APP_UI_TOKEN" in str(failure.value) and weak not in str(failure.value)


@pytest.mark.parametrize("field", ["app_ui_token", "codex_bridge_token"])
def test_production_rejects_patterned_credentials_without_printing_them(field):
    weak = "hunter2hunter3hunter4hunter5hunter6hunter"
    values = {"app_ui_token": "synthetic-ui-token-0123456789abcdef", field: weak}
    with pytest.raises(ValueError) as failure:
        Settings(
            _env_file=None,
            app_env="production",
            healthos_sources="none",
            database_url="postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health",
            **values,
        )
    assert field.upper() in str(failure.value) and weak not in str(failure.value)


# Login limit


def test_throttle_passes_a_burst_then_spaces_attempts_and_bounds_the_queue():
    now = [100.0]
    throttle = login_routes.LoginThrottle(clock=lambda: now[0])
    waits = [throttle.reserve() for _ in range(login_routes.LOGIN_BURST_ATTEMPTS)]
    assert waits == [0.0] * login_routes.LOGIN_BURST_ATTEMPTS
    assert throttle.reserve() == pytest.approx(login_routes.LOGIN_ATTEMPT_INTERVAL_SECONDS)
    reservations = [throttle.reserve() for _ in range(200)]
    assert None in reservations
    queued = [wait for wait in reservations if wait is not None]
    assert max(queued) <= login_routes.LOGIN_MAX_WAIT_SECONDS
    assert len(queued) == pytest.approx(
        login_routes.LOGIN_MAX_WAIT_SECONDS / login_routes.LOGIN_ATTEMPT_INTERVAL_SECONDS, abs=2
    )


def test_a_flood_leaves_no_lockout_once_it_has_passed():
    now = [100.0]
    throttle = login_routes.LoginThrottle(clock=lambda: now[0])
    for _ in range(500):
        throttle.reserve()
    assert throttle.reserve() is None
    now[0] += login_routes.LOGIN_MAX_WAIT_SECONDS + login_routes.LOGIN_ATTEMPT_INTERVAL_SECONDS * 2
    assert throttle.reserve() == 0.0


async def test_concurrent_guesses_are_cut_off_without_reading_the_token(application, monkeypatch):
    monkeypatch.setattr(login_routes, "LOGIN_ATTEMPT_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(login_routes, "LOGIN_MAX_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(login_routes, "FAILED_LOGIN_DELAY_SECONDS", 0)
    # A stopped clock: the admitted count depends on the slots, not on how fast this machine is.
    application.state.login_throttle = login_routes.LoginThrottle(clock=lambda: 1_000.0)
    async with client(application) as http:
        responses = await asyncio.gather(
            *(http.post("/login", data={"token": f"guess-{i}"}) for i in range(400)),
            http.post("/login", data={"token": TOKEN}),
        )
    statuses = [response.status_code for response in responses]
    assert statuses.count(429) >= 384
    wrong = [r for r in responses[:400] if r.status_code == 401]
    assert len(wrong) <= 5 + 11  # the burst and the bounded queue, not the 400 sent at once
    limited = next(r for r in responses if r.status_code == 429)
    assert limited.headers["retry-after"].isdigit() and "Çok fazla" in limited.text


async def test_login_succeeds_again_after_the_queue_drains(application, monkeypatch):
    monkeypatch.setattr(login_routes, "LOGIN_ATTEMPT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(login_routes, "LOGIN_MAX_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(login_routes, "FAILED_LOGIN_DELAY_SECONDS", 0)
    async with client(application) as http:
        await asyncio.gather(*(http.post("/login", data={"token": "x"}) for _ in range(100)))
        await asyncio.sleep(0.08)
        response = await http.post("/login", data={"token": TOKEN})
    assert response.status_code == 303


# Session lifetime on hosts that share cookies across ports


@pytest.mark.parametrize(
    ("base", "lifetime"),
    [
        ("http://localhost:8000", login_routes.UNISOLATED_HOST_SESSION_SECONDS),
        ("http://127.0.0.1:8000", login_routes.UNISOLATED_HOST_SESSION_SECONDS),
        ("http://healthos.localhost:8000", auth.COOKIE_MAX_AGE),
    ],
)
async def test_session_lifetime_follows_the_host(application, base, lifetime):
    async with client(application, base) as http:
        response = await http.post("/login", data={"token": TOKEN, "next": "/"})
    assert response.status_code == 303
    attributes = {part.strip().lower() for part in response.headers["set-cookie"].split(";")}
    assert f"max-age={lifetime}" in attributes
    assert "secure" not in attributes


async def test_a_secure_cookie_keeps_the_full_lifetime_and_its_attribute(monkeypatch):
    # An https APP_PUBLIC_URL: the Secure-cookie path is left exactly as it was.
    application = build(monkeypatch, app_public_url="https://ui.example.org")
    async with client(application, BASE) as http:
        response = await http.post("/login", data={"token": TOKEN, "next": "/"})
    attributes = {part.strip().lower() for part in response.headers["set-cookie"].split(";")}
    assert f"max-age={auth.COOKIE_MAX_AGE}" in attributes and "secure" in attributes


async def test_session_store_ends_a_short_session_when_it_should(application):
    store = application.state.ui_sessions
    now = [1_000.0]
    store.clock = lambda: now[0]
    async with client(application) as http:
        response = await http.post("/login", data={"token": TOKEN})
    session = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    now[0] += login_routes.UNISOLATED_HOST_SESSION_SECONDS - 1
    assert store.valid(session)
    now[0] += 2
    assert not store.valid(session)


async def test_login_page_states_the_session_lifetime(application):
    async with client(application) as http:
        short = await http.get("/login")
    assert "24 saat" in short.text and "30 gün" not in short.text
    async with client(application, "http://healthos.localhost:8000") as http:
        full = await http.get("/login")
    assert "30 gün" in full.text


# Logout closes the chat


class ClosingBrowser(Browser):
    def __init__(self):
        super().__init__()
        self.close_code = None

    async def close(self, code):
        self.close_code = code


async def test_a_revoked_session_closes_the_socket_at_once():
    browser, rpc = ClosingBrowser(), RPC()
    revoked = asyncio.Event()
    task = asyncio.create_task(
        chat_relay.relay(browser, rpc, "site-thread", authorized=lambda: True, revoked=revoked)
    )
    await asyncio.sleep(0)
    revoked.set()
    await asyncio.wait_for(task, 5)
    assert browser.close_code == 1008


async def test_revocation_wins_over_a_bridge_message_ready_at_the_same_moment():
    browser, rpc = ClosingBrowser(), RPC()
    revoked = asyncio.Event()
    revoked.set()
    await rpc.incoming.put(
        {"method": "item/agentMessage/delta", "params": {"threadId": "site-thread", "delta": "x"}}
    )
    await asyncio.wait_for(
        chat_relay.relay(browser, rpc, "site-thread", authorized=lambda: True, revoked=revoked), 5
    )
    assert browser.close_code == 1008 and browser.outgoing.empty()


async def test_a_bridge_message_for_a_signed_out_browser_is_not_forwarded():
    browser, rpc = ClosingBrowser(), RPC()
    await rpc.incoming.put(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "site-thread", "itemId": "i", "delta": "after logout"},
        }
    )
    await asyncio.wait_for(
        chat_relay.relay(browser, rpc, "site-thread", authorized=lambda: False), 5
    )
    assert browser.close_code == 1008 and browser.outgoing.empty()


async def test_logging_out_closes_the_open_chat_socket_at_once(monkeypatch):
    """The whole path from the chat endpoint: POST /logout revokes the session."""
    from test_codex_chat import SiteSocket, SyntheticBridge

    from app.api import codex

    transport = SyntheticBridge()
    monkeypatch.setattr(codex, "connect", lambda *args, **kwargs: transport)
    site = SiteSocket(token="synthetic")
    store = auth.SessionStore()
    site.app.state.settings.app_ui_token = "ui-token"
    site.app.state.ui_sessions = store
    session = store.issue()
    site.scope["headers"].append((b"cookie", f"{auth.COOKIE_NAME}={session}".encode()))
    assert (await site.start())["type"] == "websocket.accept"
    assert (await site.message())["type"] == "ready"
    store.revoke(session)
    frame = await site.frame()
    assert frame["type"] == "websocket.close" and frame["code"] == 1008
    await asyncio.wait_for(site.task, 30)
    assert transport.closed


def test_session_store_notifies_watchers_when_a_session_ends():
    store = auth.SessionStore()
    calls: list[str] = []
    session = store.issue()
    stop = store.watch(session, lambda: calls.append("revoked"))
    other = store.issue()
    store.watch(other, lambda: calls.append("other"))
    store.revoke(session)
    assert calls == ["revoked"]
    stop()  # after the end: harmless
    store.revoke(session)  # only once
    assert calls == ["revoked"]


def test_session_store_notifies_on_eviction_and_expiry():
    now = [0.0]
    store = auth.SessionStore(ttl_seconds=10, clock=lambda: now[0])
    calls: list[str] = []
    expiring = store.issue()
    store.watch(expiring, lambda: calls.append("expired"))
    now[0] = 11.0
    assert not store.valid(expiring)
    assert calls == ["expired"]
    store = auth.SessionStore()
    oldest = store.issue()
    store.watch(oldest, lambda: calls.append("evicted"))
    for _ in range(auth.SessionStore.MAX_SESSIONS):
        store.issue()
    assert calls == ["expired", "evicted"]


# No caching of health data


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/measurements-probe",
        "/health/probe",
        "/sync/probe",
        "/backfill/probe",
        "/google-health/probe",
    ],
)
def test_private_data_paths_are_never_stored(monkeypatch, path):
    application = build(monkeypatch)

    @application.get(path)
    async def probe():
        return {"marker": "health-value"}

    http = TestClient(application, base_url=BASE)
    response = http.get(path, headers={"authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_a_route_that_sets_its_own_caching_keeps_it(monkeypatch):
    from starlette.responses import PlainTextResponse

    application = build(monkeypatch)

    @application.get("/api/export-probe")
    async def probe():
        return PlainTextResponse("x", headers={"Cache-Control": "private, max-age=30"})

    http = TestClient(application, base_url=BASE)
    response = http.get("/api/export-probe", headers={"authorization": f"Bearer {TOKEN}"})
    assert response.headers["cache-control"] == "private, max-age=30"


def test_real_dashboard_prefix_is_covered():
    assert "/api/dashboard/measurements".startswith(NO_STORE_PREFIXES)
    assert not "/assets/app.js".startswith(NO_STORE_PREFIXES)


# Database role


async def _health(risk, sessions, *, production=True):
    settings = SimpleNamespace(
        production=production,
        healthos_sources=["none"],
        google_enabled=False,
        garmin_enabled=False,
        app_ui_token="x",
        watchdog_enabled=False,
        backup_status_file=None,
        api_read_statement_timeout_seconds=25,
    )
    snapshot = SimpleNamespace(as_dict=lambda **_: {"status": "healthy", "reasons": []})
    runtime = SimpleNamespace(
        scheduler_needed=lambda _settings: False,
        scheduler_running=False,
        pipeline_health=SimpleNamespace(
            inspect=AsyncMock(return_value=snapshot), sessions=sessions
        ),
    )
    request = SimpleNamespace(
        method="GET",
        app=SimpleNamespace(
            state=SimpleNamespace(runtime=runtime, settings=settings, database_role_risk=risk)
        ),
    )
    return await health_api.health(request)


async def test_health_names_an_unrestricted_database_role_without_failing(sessions):
    payload = await _health("superuser", sessions)
    assert payload["status"] == "healthy"
    assert payload["reasons"] == ["database_role_superuser"]


async def test_health_is_quiet_for_a_restricted_role(sessions):
    assert (await _health(None, sessions))["reasons"] == []


async def test_startup_warns_about_a_superuser_role_inside_its_grace(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app import main

    application = build(monkeypatch)
    application.state.settings.app_env = "production"
    # Without this opt-in production refuses to start (tests/test_superuser_runtime_refusal.py).
    until = datetime.now(UTC).date() + timedelta(days=2)
    application.state.settings.healthos_allow_superuser_until = until
    risk = AsyncMock(return_value=role_check.SUPERUSER)
    monkeypatch.setattr(main, "database_role_risk", risk)

    class FakeSession:
        # Already bound: the lifespan leaves a factory it did not bind to its owner.
        kw = {"bind": object()}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, _statement):
            return None

    monkeypatch.setattr(main, "SessionLocal", FakeSession)
    application.state.runtime.scheduler_needed = lambda _settings: False
    application.state.runtime.shutdown = AsyncMock()
    application.state.settings.healthos_sources = ["none"]
    warning = MagicMock()
    monkeypatch.setattr(main.logger, "warning", warning)
    async with application.router.lifespan_context(application):
        pass
    assert application.state.database_role_risk == role_check.SUPERUSER
    message, *arguments = warning.call_args_list[-1].args
    assert message.startswith("The application connects") and arguments == [until.isoformat()]
    assert "POSTGRES_APP_USER" in message
    assert application.state.database_role_grace_until == until
