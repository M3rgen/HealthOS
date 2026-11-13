"""Plain localhost sends page navigations to healthos.localhost, which has its own cookie jar."""

import httpx
import pytest
from support.auth import TOKEN, build

from app.core.canonical_host import isolated_url, redirect_enabled

LOCAL = "http://localhost:8000"
ISOLATED = "http://healthos.localhost:8000"
PAGE = {"Accept": "text/html"}


def client(application, base=LOCAL):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url=base)


@pytest.mark.parametrize(
    "path, target",
    [
        ("/", ISOLATED + "/"),
        ("/login?next=%2Fsettings", ISOLATED + "/login?next=%2Fsettings"),
        ("/page-probe?x=1&y=2", ISOLATED + "/page-probe?x=1&y=2"),
    ],
)
@pytest.mark.parametrize("base", [LOCAL, "http://127.0.0.1:8000"])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_page_navigation_moves_to_the_isolated_host(
    monkeypatch, path, target, base, authenticated
):
    async with client(build(monkeypatch), base) as http:
        headers = {**PAGE, **({"Authorization": f"Bearer {TOKEN}"} if authenticated else {})}
        response = await http.get(path, headers=headers)
    assert response.status_code == 307
    assert response.headers["location"] == target
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


async def test_navigation_header_and_port_carry_over(monkeypatch):
    async with client(build(monkeypatch), "http://localhost:18080") as http:
        response = await http.get("/", headers={"Sec-Fetch-Mode": "navigate"})
    assert response.headers["location"] == "http://healthos.localhost:18080/"


@pytest.mark.parametrize(
    "method, path, headers",
    [
        ("GET", "/api/probe", PAGE),  # API calls stay; the page that made them has moved.
        ("GET", "/api/probe", {}),
        ("GET", "/sync/status", PAGE),
        ("GET", "/backfill/status", PAGE),
        ("GET", "/google-health/capabilities", PAGE),
        ("GET", "/health/live", PAGE),  # monitors
        ("GET", "/health", PAGE),
        ("GET", "/assets/probe.js", PAGE),
        ("GET", "/models/probe.glb", PAGE),
        ("POST", "/webhooks/google-health", PAGE),
        ("GET", "/auth/google/callback?code=c&state=s", PAGE),  # Google's registered origin
        ("GET", "/auth/google/handoff?ticket=t", PAGE),
        ("GET", "/page-probe", {"Accept": "application/json"}),  # scripts and curl
        ("POST", "/login", PAGE),
    ],
)
@pytest.mark.parametrize("base", [LOCAL, "http://127.0.0.1:8000"])
async def test_api_monitor_oauth_and_writes_stay_on_localhost(
    monkeypatch, method, path, headers, base
):
    async with client(build(monkeypatch), base) as http:
        response = await http.request(method, path, headers=headers)
    assert response.status_code != 307
    if path == "/health/live":
        assert response.status_code == 200


@pytest.mark.parametrize("host", ["[::1]:8000", "healthos.localhost:8000"])
async def test_other_local_hosts_are_left_alone(monkeypatch, host):
    async with client(build(monkeypatch)) as http:
        response = await http.get("/page-probe", headers={**PAGE, "Host": host})
    assert response.status_code == 303  # straight to the login page on that host
    assert response.headers["location"].startswith("/login")


async def test_webhook_host_is_never_redirected(monkeypatch):
    async with client(build(monkeypatch)) as http:
        response = await http.get("https://hooks.example/webhooks/google-health", headers=PAGE)
    assert response.status_code == 400  # host validation answers first


async def test_explicit_localhost_address_keeps_its_session(monkeypatch):
    """APP_PUBLIC_URL on localhost is the operator's choice: no move, the cookie keeps working."""
    application = build(monkeypatch, app_public_url=LOCAL)
    async with client(application) as http:
        login = await http.post("/login", data={"token": TOKEN, "next": "/page-probe"})
        assert login.status_code == 303
        page = await http.get("/page-probe", headers=PAGE)
    assert page.status_code == 200 and page.json() == {"reached": True}


@pytest.mark.parametrize(
    "configured, enabled",
    [
        (None, True),
        ("", True),
        ("http://healthos.localhost:8000", True),
        ("http://localhost:8000", False),
        ("https://health.example", False),
    ],
)
def test_redirect_follows_the_configured_interface_address(configured, enabled):
    assert redirect_enabled(configured) is enabled


def test_isolated_url_only_rewrites_the_shared_host():
    assert isolated_url("localhost", "/x") == "http://healthos.localhost/x"
    assert isolated_url("LOCALHOST:9", "/") == "http://healthos.localhost:9/"
    assert isolated_url("127.0.0.1:8000", "/") == "http://healthos.localhost:8000/"
    assert isolated_url("localhost.attacker.example", "/") is None
    assert isolated_url("localhost:8000@evil", "/") is None


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example",
        "https://evil.example/x",
        "/https://evil.example",
        "/%2f/evil.example",
        "/\\evil.example",
    ],
)
async def test_unsafe_navigation_paths_are_rejected(monkeypatch, path):
    application = build(monkeypatch)
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost:8000"), (b"accept", b"text/html")],
    }
    messages = []

    async def send(message):
        messages.append(message)

    await application(scope, None, send)
    assert messages[0]["status"] == 400
    assert not any(key == b"location" for key, _ in messages[0]["headers"])


@pytest.mark.parametrize(
    "host",
    ["localhost.evil.example", "evil.example", "localhost:8000@evil.example", "localhost:99999"],
)
async def test_untrusted_host_never_supplies_a_redirect(monkeypatch, host):
    async with client(build(monkeypatch)) as http:
        response = await http.get("/", headers={**PAGE, "Host": host})
    assert response.status_code == 400 and "location" not in response.headers


@pytest.mark.parametrize(
    "host, link",
    [
        ("127.0.0.1:8000", "http://healthos.localhost:8000/login?next=%2Fsettings"),
        ("[::1]:9000", "http://healthos.localhost:9000/login?next=%2Fsettings"),
        ("healthos.localhost:8000", None),
    ],
)
async def test_login_page_points_shared_hosts_to_the_isolated_one(monkeypatch, host, link):
    async with client(build(monkeypatch)) as http:
        response = await http.get("/login?next=/settings", headers={"Host": host})
    assert response.status_code == 200
    if link is None:
        assert "healthos.localhost</a>" not in response.text
    else:
        assert f'<a href="{link}">healthos.localhost</a>' in response.text


async def test_login_hint_stays_off_when_the_interface_is_https(monkeypatch):
    application = build(monkeypatch, app_public_url="https://health.example")
    async with client(application) as http:
        response = await http.get("/login", headers={"Host": "127.0.0.1:8000"})
    assert "healthos.localhost</a>" not in response.text
