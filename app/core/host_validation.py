from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.apple_paths import APPLE_RECEIVER_PATH

WEBHOOK_PATH = "/webhooks/google-health"
# healthos.localhost resolves to loopback in current browsers and gets its own cookie jar,
# apart from every other server on localhost (cookies are not isolated by port).
LOCAL_HOSTS = frozenset({"localhost", "healthos.localhost", "127.0.0.1", "[::1]"})
AUTHORITY = re.compile(r"(?P<host>\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?")


def hostname(authority: str) -> str | None:
    match = AUTHORITY.fullmatch(authority)
    if match is None:
        return None
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return match.group("host").lower()


def endpoint_host(uri: str | None, path: str) -> str | None:
    """The host of an https endpoint URI ending in ``path``; None for anything else."""
    if not uri:
        return None
    try:
        endpoint = urlsplit(uri)
        if endpoint.scheme == "https" and endpoint.path == path:
            return hostname(endpoint.netloc)
    except ValueError:
        pass  # Invalid configuration must not expand the trusted host set.
    return None


class HostValidationMiddleware:
    """Keep private routes local; public webhook hosts get only their own receiver.

    The Google webhook host reaches only ``POST WEBHOOK_PATH``; the Apple receiver host
    (APPLE_RECEIVER_ENDPOINT_URI) reaches only ``POST APPLE_RECEIVER_PATH``. When both are
    the same host, each path is admitted, nothing else.
    """

    def __init__(
        self,
        app: ASGIApp,
        webhook_endpoint_uri: str | None = None,
        apple_endpoint_uri: str | None = None,
    ) -> None:
        self.app = app
        self.public = {
            path: host
            for path, host in (
                (WEBHOOK_PATH, endpoint_host(webhook_endpoint_uri, WEBHOOK_PATH)),
                (APPLE_RECEIVER_PATH, endpoint_host(apple_endpoint_uri, APPLE_RECEIVER_PATH)),
            )
            if host is not None
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
        host = hostname(hosts[0].decode("latin-1")) if len(hosts) == 1 else None
        local = host in LOCAL_HOSTS
        public_webhook = (
            host is not None
            and scope["type"] == "http"
            and scope["method"] == "POST"
            and self.public.get(scope["path"]) == host
        )
        if local or public_webhook:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "Invalid host"})
        else:
            await PlainTextResponse("Invalid host", status_code=400)(scope, receive, send)
