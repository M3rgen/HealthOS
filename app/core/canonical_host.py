"""Send browser navigations on plain ``localhost`` to ``healthos.localhost``.

A cookie cannot be scoped to a port: every other server the browser reaches on ``localhost``
receives a session issued there. ``healthos.localhost`` resolves to loopback in current browsers
and operating systems and has a cookie jar of its own, so the interface lives there.

This middleware moves only page navigations, before authentication; the login handler refuses
plain loopback logins without issuing a session. API calls, the WebSocket, health checks and Google
OAuth paths stay where they are: existing installations may explicitly register their callback on
``localhost``. The public webhook host never reaches this point (HostValidationMiddleware). An
operator who set ``APP_PUBLIC_URL`` to another address keeps it; that is the explicit way to stay
on ``localhost``.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.auth import wants_html
from app.core.host_validation import AUTHORITY, hostname

ISOLATED_HOST = "healthos.localhost"
SHARED_HOSTS = frozenset({"localhost", "127.0.0.1"})
# OAuth callbacks, probes, API clients and assets keep their registered/requested origin.
STAYING_ROOTS = (
    "/auth/google",
    "/health",
    "/api",
    "/sync",
    "/backfill",
    "/google-health",
    "/webhooks",
    "/assets",
    "/models",
)


def redirect_enabled(app_public_url: str | None) -> bool:
    """On unless APP_PUBLIC_URL names an interface address other than healthos.localhost."""
    if not app_public_url:
        return True
    try:
        return urlsplit(app_public_url).hostname == ISOLATED_HOST
    except ValueError:
        return False


def isolated_url(
    authority: str,
    path_and_query: str,
    scheme: str = "http",
    shared: frozenset[str] = SHARED_HOSTS,
) -> str | None:
    """The same address on healthos.localhost (same port), or None for any other host."""
    match = AUTHORITY.fullmatch(authority)
    if match is None or hostname(authority) not in shared:
        return None
    path = unquote(path_and_query.split("?", 1)[0])
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or "://" in path
        or any(ord(char) < 32 or ord(char) == 127 for char in path_and_query)
        or scheme not in {"http", "https"}
    ):
        raise ValueError("Invalid navigation target")
    port = match.group("port")
    return f"{scheme}://{ISOLATED_HOST}{':' + port if port else ''}{path_and_query}"


class CanonicalHostMiddleware:
    def __init__(self, app: ASGIApp, enabled: bool = True) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            target = self._target(scope) if self.enabled and scope["type"] == "http" else None
        except ValueError:
            await PlainTextResponse("Invalid navigation target", status_code=400)(
                scope, receive, send
            )
            return
        if target is None:
            await self.app(scope, receive, send)
            return
        response = RedirectResponse(target, status_code=307, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)

    @staticmethod
    def _target(scope: Scope) -> str | None:
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or not wants_html(scope, headers):
            return None
        if any(
            scope["path"] == root or scope["path"].startswith(root + "/") for root in STAYING_ROOTS
        ):
            return None
        raw_path = scope.get("raw_path") or scope["path"].encode()
        query = scope.get("query_string") or b""
        location = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
        return isolated_url(hosts[0], location, scope.get("scheme", "http"))
