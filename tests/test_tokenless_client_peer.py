"""Tokenless mode trusts the client's address only, never the listener's.

A forwarded port (an SSH or IDE port-forward, a NAT rule, Docker Desktop) can put a request from
another host onto a loopback listener; with login off it must still be refused.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from support.auth import BASE, build

REFUSAL = {"detail": "Giriş kapalıyken yalnız bu bilgisayardan erişilebilir."}


def on_listener(application, server):
    async def scoped(scope, receive, send):
        await application({**scope, "server": server}, receive, send)

    return scoped


@pytest.mark.parametrize(
    "server,peer",
    [
        (("127.0.0.1", 8000), "192.0.2.50"),
        (("127.0.0.1", 8000), "172.17.0.3"),
        (("::1", 8000), "2001:db8::50"),
        (("127.0.0.1", 8000), "::ffff:192.0.2.50"),
        (("127.0.0.1", 8000), "testclient"),
    ],
)
async def test_loopback_listener_does_not_admit_a_remote_client(monkeypatch, server, peer):
    application = on_listener(build(monkeypatch, token=None), server)
    transport = httpx.ASGITransport(app=application, client=(peer, 40000))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        for path in ("/api/probe", "/health"):
            response = await http.get(path)
            assert response.status_code == 403, path
            assert response.json() == REFUSAL


async def test_loopback_client_keeps_the_documented_local_use(monkeypatch):
    application = on_listener(build(monkeypatch, token=None), ("127.0.0.1", 8000))
    for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        transport = httpx.ASGITransport(app=application, client=(peer, 40000))
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            assert (await http.get("/api/probe")).json() == {"reached": True}


def test_forwarded_websocket_from_another_host_is_closed(monkeypatch):
    application = on_listener(build(monkeypatch, token=None), ("127.0.0.1", 8000))
    http = TestClient(application, base_url=BASE, client=("192.0.2.50", 40000))
    with pytest.raises(WebSocketDisconnect) as closed:
        with http.websocket_connect("ws://localhost:8000/api/codex/chat", headers={"Origin": BASE}):
            pass
    assert (closed.value.code, closed.value.reason) == (1008, "loopback_required")


async def test_public_routes_stay_reachable_for_a_remote_client(monkeypatch):
    application = on_listener(build(monkeypatch, token=None), ("127.0.0.1", 8000))
    transport = httpx.ASGITransport(app=application, client=("192.0.2.50", 40000))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        assert (await http.get("/health/live")).status_code == 200
