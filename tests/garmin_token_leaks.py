"""Helpers for the Garmin bearer-token redaction tests (unit and PostgreSQL).

``SecretEchoGarmin`` is the usual fake Personal API with knobs that put secrets where a careless
capture would keep them: credential headers on the token answer, a token exchange error whose
body echoes the key, a 401 whose body echoes the bearer token. ``leaks`` reads every column of
every ``garmin_api_responses`` row, as bytes and as JSON, and names the columns holding a secret.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from garmin_records import FakeGarmin
from sqlalchemy import text

from app.garmin.client import TOKEN_PATH


class SecretEchoGarmin(FakeGarmin):
    def __init__(self) -> None:
        super().__init__()
        # Extra headers on the token answer (credential-shaped headers a capture must not keep).
        self.token_headers: dict[str, str] = {}
        # The body of a refused token exchange (default: empty, like FakeGarmin).
        self.token_error_body: bytes = b""
        # Paths whose 401 answer echoes the bearer token of the request in its body.
        self.echo_401: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == TOKEN_PATH:
            self.requests.append(request)
            self.token_requests += 1
            headers = {"content-type": "text/plain", **self.token_headers}
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status, content=self.token_error_body, headers=headers
                )
            return httpx.Response(200, text=self.token, headers=headers)
        queue = self.statuses.get(path)
        if path in self.echo_401 and queue and queue[0] == 401:
            queue.pop(0)
            self.requests.append(request)
            bearer = request.headers["authorization"].removeprefix("Bearer ")
            return httpx.Response(
                401,
                json={"error": "invalid_token", "token": bearer},
                headers={"www-authenticate": f'Bearer error="invalid_token", token="{bearer}"'},
            )
        return super().__call__(request)


def needles(*secrets: str) -> list[bytes]:
    """The secrets, and each dot-separated part of a JWT long enough to identify it."""
    found: list[bytes] = []
    for secret in secrets:
        found.append(secret.encode())
        found.extend(part.encode() for part in secret.split(".") if len(part) >= 16)
    return found


def _bytes(value: Any) -> bytes:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, str):
        return value.encode()
    return json.dumps(value, default=str).encode()


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        return {"latin1": raw.decode("latin-1"), "base64": base64.b64encode(raw).decode()}
    return value


def scan(rows: list[dict[str, Any]], *secrets: str) -> list[str]:
    """``id:column`` of every value holding a secret, then ``id:row`` if the row as JSON does."""
    hits: list[str] = []
    patterns = needles(*secrets)
    for row in rows:
        ident = row.get("id")
        for column, value in row.items():
            data = _bytes(value)
            if any(pattern in data for pattern in patterns):
                hits.append(f"{ident}:{column}")
        whole = json.dumps({k: _jsonable(v) for k, v in row.items()}, default=str).encode()
        if any(pattern in whole for pattern in patterns):
            hits.append(f"{ident}:row")
    return hits


async def archived(sessions) -> list[dict[str, Any]]:
    """Every ``garmin_api_responses`` row, every column, as the database returns it."""
    async with sessions() as session:
        result = await session.execute(text("SELECT * FROM garmin_api_responses ORDER BY id"))
        return [dict(row._mapping) for row in result]


async def leaks(sessions, *secrets: str) -> list[str]:
    return scan(await archived(sessions), *secrets)
