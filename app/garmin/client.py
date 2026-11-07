"""A thin, read-only client for the Garmin Connect Personal API.

The official ``garmin-connect-personal-api-python`` package depends on ``requests`` and writes
its bearer token to a cache file; this project pins its dependencies (``uv.lock``) and already
has ``httpx``, so the few calls it needs are written here, following the official client's
paths. Behaviour:

* The Personal API key is read from a file (``GARMIN_API_KEY_FILE``) on every token exchange
  and kept in no attribute, log, response row or error message. It is exchanged for a bearer
  token (a JWT valid for 24 hours, no refresh) with ``POST .../personal-api-key/token`` and
  ``Authorization: ApiKey <key>``; the response is the token as plain text. The token lives in
  memory only and is renewed an hour before it expires, or when a request answers 401 (once).
* Every call is a GET. Requests are paced (``MIN_INTERVAL``) and retried with exponential
  back-off on 429 and 5xx, honouring ``Retry-After`` up to ``MAX_RETRY_AFTER``; the rate limit
  is not published. A 400 is a range or parameter error and is not retried.
* Every HTTP exchange that produced a response is captured (:class:`GarminExchange`) with the
  body byte for byte, the request path and query (never a header), the status and a few
  response headers, so the pull can archive it in ``garmin_api_responses``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.logging import event

logger = logging.getLogger(__name__)

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
# Renew the bearer token this long before its ``exp``; the token itself lasts 24 hours.
TOKEN_RENEWAL_MARGIN = timedelta(hours=1)
# Gentle pacing: the Personal API publishes no rate limit.
MIN_INTERVAL = 1.0
MAX_ATTEMPTS = 5
BACKOFF_BASE = 2.0
MAX_RETRY_AFTER = 300.0
TIMEOUT = httpx.Timeout(30.0)
# Response headers kept with a body: enough to read and date it, nothing about the caller.
KEPT_HEADERS = ("content-type", "content-length", "date", "retry-after", "www-authenticate")


class GarminError(Exception):
    """Base class; messages carry no key, token or response body."""


class GarminKeyError(GarminError):
    """The API key file is missing, unreadable or empty."""


class GarminAuthError(GarminError):
    """Garmin refused the key (token exchange) or the bearer token twice in a row."""


class GarminRequestError(GarminError):
    """A 4xx other than 401/429 (e.g. a window longer than the endpoint allows)."""

    def __init__(self, status_code: int, path: str) -> None:
        super().__init__(f"Garmin answered HTTP {status_code} for {path}")
        self.status_code = status_code


class GarminUnavailableError(GarminError):
    """429/5xx or transport failures that outlasted the retries."""


@dataclass(frozen=True)
class GarminExchange:
    """One HTTP exchange with Garmin, as kept in ``garmin_api_responses``."""

    fetched_at: datetime
    kind: str
    context_id: str | None
    method: str
    path: str
    query: str | None
    attempt: int
    status_code: int
    headers: dict[str, str]
    body: bytes
    complete: bool = True
    error: str | None = None
    sha256: str = field(init=False)
    request_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sha256", hashlib.sha256(self.body).hexdigest())
        request = f"{self.method} {self.path}" + (f"?{self.query}" if self.query else "")
        object.__setattr__(self, "request_sha256", hashlib.sha256(request.encode()).hexdigest())

    def row(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "kind": self.kind,
            "context_id": self.context_id,
            "method": self.method,
            "path": self.path,
            "query": self.query,
            "request_sha256": self.request_sha256,
            "attempt": self.attempt,
            "status_code": self.status_code,
            "headers": self.headers,
            "body": self.body,
            "body_sha256": self.sha256,
            "body_size": len(self.body),
            "complete": self.complete,
            "error": self.error,
        }


@dataclass
class GarminResponse:
    """A successful GET: the decoded JSON and every exchange it took (retries included)."""

    json: Any
    exchanges: list[GarminExchange]

    @property
    def exchange(self) -> GarminExchange:
        return self.exchanges[-1]


def read_api_key(path: Path) -> str:
    """The key from its file, stripped. Only the path appears in an error."""
    try:
        key = path.expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise GarminKeyError(f"Garmin API key file not found: {path}") from None
    except OSError as exc:
        raise GarminKeyError(f"Garmin API key file unreadable: {path}") from exc
    if not key or any(character.isspace() for character in key):
        raise GarminKeyError(f"Garmin API key file is empty or malformed: {path}")
    return key


def token_expiry(token: str, now: datetime) -> datetime:
    """The ``exp`` claim of the (unverified) JWT; a day from now when it cannot be read."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
    except (IndexError, KeyError, TypeError, ValueError):
        return now + timedelta(hours=24)


def retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def backoff(attempt: int) -> float:
    return float(BACKOFF_BASE**attempt)


def kept_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: headers[name] for name in KEPT_HEADERS if name in headers}


class GarminClient:
    def __init__(
        self,
        key_file: Path,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], datetime] | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.key_file = key_file
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT)
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(UTC))
        self.base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._last_request_at: datetime | None = None

    async def aclose(self) -> None:
        await self.http.aclose()

    @property
    def key_file_present(self) -> bool:
        return self.key_file.expanduser().is_file()

    async def token(self, exchanges: list[GarminExchange], *, force: bool = False) -> str:
        """The bearer token, renewed when missing, forced, or within an hour of its expiry."""
        now = self.clock()
        if (
            not force
            and self._token is not None
            and self._expires_at is not None
            and self._expires_at - now > TOKEN_RENEWAL_MARGIN
        ):
            return self._token
        key = read_api_key(self.key_file)
        await self._pace()
        response = await self.http.post(
            f"{self.base_url}{TOKEN_PATH}",
            headers={"Authorization": f"ApiKey {key}", "Accept": "*/*"},
        )
        del key
        exchanges.append(
            self._capture("token", None, "POST", TOKEN_PATH, None, 0, response, complete=True)
        )
        if response.status_code != 200 or not response.text.strip():
            event(logger, "GARMIN_TOKEN_REFUSED", status_code=response.status_code)
            raise GarminAuthError(f"Garmin refused the API key (HTTP {response.status_code})")
        self._token = response.text.strip()
        self._expires_at = token_expiry(self._token, now)
        event(logger, "GARMIN_TOKEN_OBTAINED", expires_at=self._expires_at.isoformat())
        return self._token

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        kind: str,
        context_id: str | None = None,
        exchanges: list[GarminExchange] | None = None,
    ) -> GarminResponse:
        """GET ``path``; retries 429/5xx with back-off, renews the token once on 401.

        ``exchanges``: a caller-owned list every exchange is appended to, so the caller can
        archive the responses of a request that ends in an error as well."""
        query = urlencode(sorted((k, str(v)) for k, v in (params or {}).items())) or None
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        if exchanges is None:
            exchanges = []
        renewed = False
        attempt = 0
        while True:
            token = await self.token(exchanges, force=renewed and attempt == 0)
            await self._pace()
            try:
                response = await self.http.get(
                    url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
                )
            except httpx.HTTPError as exc:
                event(logger, "GARMIN_REQUEST_FAILED", kind=kind, error_type=type(exc).__name__)
                if attempt + 1 >= MAX_ATTEMPTS:
                    raise GarminUnavailableError(f"Garmin unreachable for {path}") from exc
                await self.sleep(backoff(attempt))
                attempt += 1
                continue
            exchanges.append(self._capture(kind, context_id, "GET", path, query, attempt, response))
            status = response.status_code
            if status in (200, 204):
                # 204 (seen live for a day without HRV yet) and an empty 200 body: no data.
                try:
                    payload = response.json() if response.content.strip() else None
                except ValueError as exc:
                    raise GarminRequestError(status, path) from exc
                return GarminResponse(payload, exchanges)
            if status == 401 and not renewed:
                # The token expired or was revoked early: one fresh exchange, then give up.
                renewed = True
                attempt = 0
                self._token = None
                continue
            if status == 401:
                raise GarminAuthError("Garmin refused the bearer token twice")
            if status == 429 or 500 <= status < 600:
                delay = retry_after(response.headers)
                if delay is None:
                    delay = backoff(attempt)
                event(
                    logger,
                    "GARMIN_RATE_LIMITED" if status == 429 else "GARMIN_SERVER_ERROR",
                    kind=kind,
                    status_code=status,
                    retry_after_seconds=delay,
                )
                if delay > MAX_RETRY_AFTER or attempt + 1 >= MAX_ATTEMPTS:
                    raise GarminUnavailableError(f"Garmin answered HTTP {status} for {path}")
                await self.sleep(delay)
                attempt += 1
                continue
            raise GarminRequestError(status, path)

    async def _pace(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            elapsed = (now - self._last_request_at).total_seconds()
            if elapsed < MIN_INTERVAL:
                await self.sleep(MIN_INTERVAL - elapsed)
        self._last_request_at = self.clock()

    def _capture(
        self,
        kind: str,
        context_id: str | None,
        method: str,
        path: str,
        query: str | None,
        attempt: int,
        response: httpx.Response,
        *,
        complete: bool = True,
    ) -> GarminExchange:
        return GarminExchange(
            fetched_at=self.clock(),
            kind=kind,
            context_id=context_id,
            method=method,
            path=path,
            query=query,
            attempt=attempt,
            status_code=response.status_code,
            headers=kept_headers(response.headers),
            body=response.content,
            complete=complete,
        )
