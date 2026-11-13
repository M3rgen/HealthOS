import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_auth import BASE, TOKEN, build


def bound_app(application, server):
    async def scoped(scope, receive, send):
        await application({**scope, "server": server}, receive, send)

    return scoped


@pytest.mark.parametrize(
    "server,peer,status",
    [
        ("0.0.0.0", "198.51.100.7", 403),
        ("198.51.100.10", "198.51.100.7", 403),
        ("::", "2001:db8::7", 403),
        ("::", "::ffff:198.51.100.7", 403),
        ("localhost", "198.51.100.7", 403),
        (None, None, 403),
        ("0.0.0.0", "127.0.0.1", 200),
        ("198.51.100.10", "::1", 200),
        ("::", "::ffff:127.0.0.1", 200),
        # A loopback listener does not vouch for the client (tests/test_tokenless_client_peer.py).
        ("127.0.0.1", "198.51.100.7", 403),
        ("0:0:0:0:0:0:0:1", "2001:db8::7", 403),
    ],
)
async def test_tokenless_requests_use_actual_endpoints(monkeypatch, server, peer, status):
    application = build(monkeypatch, token=None)  # APP_HOST remains loopback.
    transport = httpx.ASGITransport(
        app=bound_app(application, (server, 8000) if server else None),
        client=(peer, 1234) if peer else None,
    )
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        response = await http.get(
            "/api/probe",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost"},
        )
    assert response.status_code == status
    if status == 403:
        assert response.json() == {
            "detail": "Giriş kapalıyken yalnız bu bilgisayardan erişilebilir."
        }
        assert response.headers["cache-control"] == "no-store"
    else:
        assert response.json() == {"reached": True}


async def test_tokenless_public_exceptions_and_private_pages(monkeypatch):
    application = build(monkeypatch, token=None)
    transport = httpx.ASGITransport(
        app=bound_app(application, ("0.0.0.0", 8000)), client=("198.51.100.7", 1234)
    )
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        for path in ("/page-probe", "/login", "/auth/google/callback", "/health"):
            # On the interface's own host: plain localhost first moves there (307).
            page = await http.get(
                "http://healthos.localhost:8000" + path, headers={"Accept": "text/html"}
            )
            assert page.status_code == 403
        assert (await http.get("/health/live")).status_code == 200
        assert (await http.head("/health/live")).status_code == 200
        hook = await http.post(
            "https://hooks.example/webhooks/google-health",
            json={"type": "verification"},
            headers={"Authorization": "Bearer webhook-secret-0123456789"},
        )
        assert hook.status_code == 201
        assert (await http.get("/webhooks/google-health")).status_code == 403


@pytest.mark.parametrize("peer", ["198.51.100.7", "127.0.0.1"])
def test_tokenless_websocket_respects_peer_address(monkeypatch, peer):
    application = bound_app(build(monkeypatch, token=None), ("0.0.0.0", 8000))
    http = TestClient(application, base_url=BASE, client=(peer, 1234))
    if peer == "127.0.0.1":
        with http.websocket_connect(
            "ws://localhost:8000/api/codex/chat", headers={"Origin": BASE}
        ) as ws:
            assert ws.receive_json()["type"] == "error"  # Local bridge is not configured.
        return
    with pytest.raises(WebSocketDisconnect) as closed:
        with http.websocket_connect("ws://localhost:8000/api/codex/chat", headers={"Origin": BASE}):
            pass
    assert (closed.value.code, closed.value.reason) == (1008, "loopback_required")


async def test_remote_token_authentication_is_unchanged(monkeypatch):
    transport = httpx.ASGITransport(
        app=bound_app(build(monkeypatch), ("0.0.0.0", 8000)), client=("198.51.100.7", 1234)
    )
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        assert (await http.get("/api/probe")).status_code == 401
        assert (
            await http.get("/api/probe", headers={"Authorization": f"Bearer {TOKEN}"})
        ).status_code == 200
