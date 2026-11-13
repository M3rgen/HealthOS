"""Single-user bearer-token barrier in front of the local UI and API."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable, Iterable
from ipaddress import IPv6Address, ip_address
from urllib.parse import urlencode, urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.apple_paths import APPLE_RECEIVER_PATH
from app.core.host_validation import WEBHOOK_PATH, hostname

COOKIE_NAME = "healthos_ui"
COOKIE_MAX_AGE = 30 * 24 * 3600
LOGIN_PATH = "/login"
LOGOUT_PATH = "/logout"
# Requests that must work without the UI token. The OAuth callback arrives as a
# cross-site redirect (SameSite=Strict cookies are withheld) and is guarded by the
# single-use HMAC-signed state and a separate Lax OAuth session cookie. The Apple
# receiver checks its own bearer token (APPLE_RECEIVER_TOKEN_FILE), never the UI token.
EXEMPT = frozenset(
    {
        ("POST", WEBHOOK_PATH),
        ("POST", APPLE_RECEIVER_PATH),
        ("GET", "/health/live"),
        ("HEAD", "/health/live"),
        ("GET", LOGIN_PATH),
        ("HEAD", LOGIN_PATH),
        # The router redirects these to LOGIN_PATH (redirect_slashes).
        ("GET", LOGIN_PATH + "/"),
        ("HEAD", LOGIN_PATH + "/"),
        ("POST", LOGIN_PATH),
        # Only ever removes a session; CSRF-checked like the login form.
        ("POST", LOGOUT_PATH),
        ("GET", "/auth/google/callback"),
        # Spends a single-use ticket an authenticated start issued (app/api/auth.py).
        ("GET", "/auth/google/handoff"),
    }
)


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


class SessionStore:
    """Login sessions: a random id per login, kept in memory with an absolute expiry.

    The cookie carries only that id, never anything derived from APP_UI_TOKEN, so a
    copied cookie stops working on logout, expiry or restart. Only a SHA-256 of each id
    is stored. Sessions do not survive a restart; for a single-user app that just means
    logging in again. The store is bounded; the oldest session is dropped first.
    """

    MAX_SESSIONS = 64

    def __init__(
        self, ttl_seconds: float = COOKIE_MAX_AGE, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl = ttl_seconds
        self.clock = clock
        self._expiry: dict[str, float] = {}
        # Called once a session ends, so an open chat socket closes with it.
        self._watchers: dict[str, set[Callable[[], None]]] = {}

    def _end(self, key: str) -> None:
        self._expiry.pop(key, None)
        for callback in self._watchers.pop(key, ()):
            callback()

    def issue(self, ttl_seconds: float | None = None) -> str:
        now = self.clock()
        for key in [key for key, expiry in self._expiry.items() if expiry <= now]:
            self._end(key)
        while len(self._expiry) >= self.MAX_SESSIONS:
            self._end(min(self._expiry, key=self._expiry.__getitem__))
        session_id = secrets.token_urlsafe(32)
        self._expiry[_digest(session_id)] = now + (self.ttl if ttl_seconds is None else ttl_seconds)
        return session_id

    def valid(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        key = _digest(session_id)
        expiry = self._expiry.get(key)
        if expiry is None:
            return False
        if expiry <= self.clock():
            self._end(key)
            return False
        return True

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._end(_digest(session_id))

    def watch(self, session_id: str, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` when this live session ends; returns the matching unsubscribe."""
        key = _digest(session_id)
        self._watchers.setdefault(key, set()).add(callback)

        def stop() -> None:
            watchers = self._watchers.get(key)
            if watchers is not None:
                watchers.discard(callback)
                if not watchers:
                    del self._watchers[key]

        return stop


def token_matches(token: str, candidate: str | None) -> bool:
    return candidate is not None and secrets.compare_digest(candidate.encode(), token.encode())


def _cookies(header: str) -> Iterable[tuple[str, str]]:
    for item in header.split(";"):
        name, separator, value = item.strip().partition("=")
        if separator:
            yield name, value


def session_cookies(headers: Headers) -> list[str]:
    return [
        value
        for header in headers.getlist("cookie")
        for name, value in _cookies(header)
        if name == COOKIE_NAME
    ]


def request_authenticated(
    token: str | None, headers: Headers, sessions: SessionStore | None
) -> bool:
    """Bearer APP_UI_TOKEN, or a login cookie naming a live session in ``sessions``."""
    if not token:
        return True
    authorization = headers.getlist("authorization")
    if len(authorization) == 1:
        scheme, _, credentials = authorization[0].partition(" ")
        if scheme.lower() == "bearer" and token_matches(token, credentials.strip()):
            return True
    return sessions is not None and any(sessions.valid(value) for value in session_cookies(headers))


def safe_next(value: str | None) -> str:
    """Return a same-site relative path, falling back to the dashboard."""
    if not value or len(value) > 2048 or not value.startswith("/"):
        return "/"
    if value.startswith(("//", "/\\")) or any(ord(c) < 0x21 or c in '\\"<>' for c in value):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc or parts.path.startswith(LOGIN_PATH):
        return "/"
    # Rebuilt rather than returned as received: the leading slash is ours and only the validated
    # remainder comes from the caller, which code scanning can see and a reader need not infer.
    return "/" + value[1:]


def same_origin_form(headers: Headers, scheme: str) -> bool:
    """CSRF check for the login form: same origin as the Host the request reached."""
    fetch_sites = {value.lower() for value in headers.getlist("sec-fetch-site")}
    if fetch_sites & {"cross-site", "same-site"}:
        return False  # same-site still includes other localhost ports.
    origins = headers.getlist("origin")
    if not origins:
        return True  # Non-browser clients; browsers always send Origin on form POSTs.
    hosts = headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1 or hostname(hosts[0]) is None:
        return False
    try:
        origin = urlsplit(origins[0])
        target = urlsplit(f"{scheme}://{hosts[0]}")
        default = 443 if scheme == "https" else 80
        return (
            origins[0] == f"{origin.scheme}://{origin.netloc}"
            and origin.scheme == target.scheme
            and origin.hostname == target.hostname
            and (origin.port or default) == (target.port or default)
            and origin.username is None
        )
    except ValueError:
        return False


def wants_html(scope: Scope, headers: Headers) -> bool:
    """A browser navigation to a page (not an API call, a script or an asset)."""
    if scope["method"] not in {"GET", "HEAD"} or scope["path"].startswith("/api/"):
        return False
    return headers.get("sec-fetch-mode") == "navigate" or "text/html" in headers.get("accept", "")


class UITokenMiddleware:
    """Require APP_UI_TOKEN (bearer header or login cookie) for every private route."""

    def __init__(self, app: ASGIApp, token: str | None, sessions: SessionStore) -> None:
        self.app = app
        self.token = token
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if not self.token:
            public = scope["type"] == "http" and (
                (scope["method"] in {"GET", "HEAD"} and scope["path"] == "/health/live")
                or (scope["method"] == "POST" and scope["path"] == WEBHOOK_PATH)
            )
            # The client address decides, not the listener: a port-forward or NAT rule that
            # lands on a loopback listener still brings the request from another host.
            if public or _loopback(scope.get("client")):
                await self.app(scope, receive, send)
            elif scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "loopback_required"})
            else:
                await JSONResponse(
                    {"detail": "Giriş kapalıyken yalnız bu bilgisayardan erişilebilir."},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            return
        headers = Headers(scope=scope)
        exempt = scope["type"] == "http" and (scope["method"], scope["path"]) in EXEMPT
        if exempt or request_authenticated(self.token, headers, self.sessions):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "auth_required"})
            return
        if wants_html(scope, headers):
            target = scope["path"]
            if scope.get("query_string"):
                target += "?" + scope["query_string"].decode("latin-1")
            response = RedirectResponse(
                f"{LOGIN_PATH}?{urlencode({'next': safe_next(target)})}",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )
        else:
            response = JSONResponse(
                {"detail": "auth_required"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="HealthOS"',
                    "Cache-Control": "no-store",
                },
            )
        await response(scope, receive, send)


def _loopback(endpoint: tuple[str, int] | None) -> bool:
    if not endpoint:
        return False
    try:
        address = ip_address(endpoint[0])
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback
