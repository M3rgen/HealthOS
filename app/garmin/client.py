"""A thin, read-only client for the Garmin Connect Personal API.

The official ``garmin-connect-personal-api-python`` package depends on ``requests`` and writes
its bearer token to a cache file; this project pins its dependencies (``uv.lock``) and already
has ``httpx``, so the few calls it needs are written here, following the official client's
paths. Behaviour:

* The Personal API key is read from a file (``GARMIN_API_KEY_FILE``) on every token exchange
  and before each GET attempt, even when the bearer token is cached. It is kept in no
  attribute, log, response row or error message. It is exchanged for a bearer
  token (a JWT valid for 24 hours, no refresh) with ``POST .../personal-api-key/token`` and
  ``Authorization: ApiKey <key>``; the response is the token as plain text. The token lives in
  memory only (never in a response row, log or error message) and is renewed an hour before it
  expires, or when a request answers 401 (once).
* Data calls are GETs. All requests are paced (``MIN_INTERVAL``) and retried with exponential
  back-off on 429 and 5xx, honouring ``Retry-After`` up to ``MAX_RETRY_AFTER``; the rate limit
  is not published. A 400 is a range or parameter error and is not retried.
* Every HTTP exchange that produced a response is captured (:class:`GarminExchange`) with the
  request path and query (never a request header), the status, a few response headers and the
  body byte for byte, so the pull can archive it in ``garmin_api_responses``. The exception is
  a credential: the body of a token exchange (whatever its status) and any body that contains
  the bearer token or the key are replaced by :data:`REDACTED_BODY`, and a kept header naming
  or carrying one is masked. ``body_sha256`` and ``body_size`` still describe the bytes Garmin
  sent, so the row stays evidence of the exchange without being a usable credential.
* A response is read under byte budgets (:mod:`app.core.bounded_http`); one over them is
  refused, archived as incomplete and never parsed.
* Invalid or ambiguous JSON raises :class:`GarminResponseDecodeError`. Captured bytes,
  size and digest stay available to error archival under the same credential masking rules.
* GET transport failures retry without retaining the HTTP exception or its request,
  which may carry the bearer token. Diagnostics keep the exception class; exhaustion
  raises :class:`GarminUnavailableError` with the path and no transport cause or context.
* A successful GET echoing the bearer token or API key, including in JSON-escaped string keys
  or values, is refused before ingestion. Only masked HTTP evidence is retained.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core import http_retry
from app.core.bounded_http import ACCEPT_ENCODING, ReadBody, read_bounded
from app.core.logging import event
from app.garmin import header_credentials, secret_scan
from app.garmin.response_json import unique_object

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
# What one response may occupy; the largest real answers are a few MiB of JSON. ``MAX_WIRE_BYTES``
# counts the bytes delivered, ``MAX_DECODED_BYTES`` what they may inflate to.
MAX_WIRE_BYTES = 64 * 1024 * 1024
MAX_DECODED_BYTES = 32 * 1024 * 1024
# A token exchange's answer is a JWT.
MAX_TOKEN_BYTES = 1024 * 1024
# Response headers kept with a body: enough to read and date it, nothing about the caller.
KEPT_HEADERS = ("content-type", "content-length", "date", "retry-after", "www-authenticate")
# The ``kind`` of the key-for-token exchange; its body is the bearer token (or an error about
# the key) and is never kept.
TOKEN_KIND = "token"
# What a capture keeps instead of a body that is, or contains, a credential.
REDACTED_BODY = b"[redacted: garmin bearer token]"
REDACTED_HEADER = "[redacted]"
# Headers that carry credentials by name; masked even if ``KEPT_HEADERS`` ever lists one.
CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-auth-token"}
)


class GarminError(Exception):
    """Base class; messages carry no key, token or response body."""


class GarminKeyError(GarminError):
    """The API key file is missing, unreadable or not safe for an HTTP header."""


class GarminAuthError(GarminError):
    """Garmin refused the key (token exchange) or the bearer token twice in a row."""


class GarminRequestError(GarminError):
    """A 4xx other than 401/429 (e.g. a window longer than the endpoint allows)."""

    def __init__(self, status_code: int, path: str) -> None:
        super().__init__(f"Garmin answered HTTP {status_code} for {path}")
        self.status_code = status_code


class GarminUnavailableError(GarminError):
    """429/5xx or transport failures that outlasted the retries."""


class GarminResponseDecodeError(GarminError):
    """A complete HTTP response whose body the JSON decoder cannot read.

    This is a decoding failure, independent of the decoded payload's outer shape.
    Retrying the same response cannot help; the caller must archive its exchanges
    before recording the kind's failure. No decoder exception is retained.
    """

    def __init__(self) -> None:
        super().__init__("Garmin response could not be decoded as JSON")


class GarminResponseRefused(GarminError):
    """A body over its byte budget or in an unasked-for encoding; retrying cannot help."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"Garmin response for {path} was refused: {reason}")
        self.reason = reason


class GarminCredentialEchoError(GarminError):
    """A credential-bearing success is evidence only; never retry or ingest it."""

    def __init__(self) -> None:
        super().__init__("Garmin response contained a credential")


def body_contains_secret(body: bytes, secrets: Sequence[str]) -> bool:
    """Match raw bytes and JSON string keys/values in O(body size) for fixed secrets.

    Byte searches, encoding detection and the string lexer each make a bounded
    number of passes. Decoded slices never overlap, so substring searches also
    have linear total input. No object traversal or recursion is needed, and no
    decoded body or credential is retained. Literal backslashes stay literal.
    """
    secrets = [secret for secret in secrets if secret]
    if any(secret.encode() in body for secret in secrets):
        return True
    if not secrets or (b"\\" not in body and b"\x00" not in body):
        return False
    # Malformed bytes elsewhere must not hide an otherwise recoverable secret.
    text = body.decode(json.detect_encoding(body), "replace")
    return any(secret in value for value in secret_scan.json_strings(text) for secret in secrets)


@dataclass(frozen=True)
class _CaptureRequest:
    kind: str
    context_id: str | None
    method: str
    path: str
    query: str | None
    attempt: int


@dataclass(frozen=True)
class GarminExchange:
    """One HTTP exchange with Garmin, as kept in ``garmin_api_responses``.

    ``sha256`` and ``size`` are over the body as received. A token exchange's body, a body
    containing one of ``secrets`` (the bearer token, the key) and a header value carrying one
    are not kept: ``body`` becomes :data:`REDACTED_BODY` and such a header
    :data:`REDACTED_HEADER`, so neither the instance nor its ``row()`` holds the credential.

    ``body_redacted`` records the masking decision independently of the placeholder
    bytes. A successful data call may archive this capture, but must refuse to
    return its original payload to ingestion.
    """

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
    # Credentials of this exchange to keep out of the body and headers (not stored).
    secrets: InitVar[Sequence[str]] = ()
    sha256: str = field(init=False)
    size: int = field(init=False)
    request_sha256: str = field(init=False)
    body_redacted: bool = field(init=False)

    def __post_init__(self, secrets: Sequence[str]) -> None:
        object.__setattr__(self, "sha256", hashlib.sha256(self.body).hexdigest())
        object.__setattr__(self, "size", len(self.body))
        request = f"{self.method} {self.path}" + (f"?{self.query}" if self.query else "")
        object.__setattr__(self, "request_sha256", hashlib.sha256(request.encode()).hexdigest())
        if self.kind == TOKEN_KIND:
            # The body is the token itself: no header may repeat it either.
            # HTTPX falls back to Latin-1 for headers containing invalid UTF-8.
            secrets = (
                *secrets,
                self.body.decode("utf-8", "replace").strip(),
                self.body.decode("latin-1").strip(),
            )
        redacted = self.kind == TOKEN_KIND or body_contains_secret(self.body, secrets)
        object.__setattr__(self, "body_redacted", redacted)
        if redacted:
            object.__setattr__(self, "body", REDACTED_BODY)
        object.__setattr__(self, "headers", redacted_headers(self.headers, secrets))

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
            "body_size": self.size,
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
    """Read and validate a fresh key; only the path appears in an error.

    Missing, malformed and undecodable files fail before any HTTP request, even
    when a cached bearer would otherwise allow GETs without a token exchange.
    """
    try:
        key = path.expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        error = f"Garmin API key file not found: {path}"
    except (OSError, UnicodeError):
        error = f"Garmin API key file unreadable: {path}"
    else:
        if header_credentials.is_header_value(key) and not any(c.isspace() for c in key):
            return key
        error = f"Garmin API key file is empty or malformed: {path}"
    # A UnicodeDecodeError retains file bytes. Leave the handler before raising
    # so even implicit exception context cannot expose key-file contents.
    raise GarminKeyError(error)


def token_expiry(token: str, now: datetime) -> datetime:
    """The unverified JWT's ``exp``, or a day from now if decoding/conversion fails.

    Limit recovery to unreadable claims and timestamps outside the platform's
    range, including the JSON decoder's nesting limit. Other failures propagate.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, RecursionError):
        return now + timedelta(hours=24)
    try:
        return datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return now + timedelta(hours=24)


def retry_after(headers: Mapping[str, str]) -> float | None:
    return http_retry.retry_after(headers.get("retry-after"), strict=False)


def backoff(attempt: int) -> float:
    return http_retry.backoff(attempt, base=BACKOFF_BASE)


def kept_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: headers[name] for name in KEPT_HEADERS if name in headers}


def redacted_headers(headers: Mapping[str, str], secrets: Sequence[str] = ()) -> dict[str, str]:
    """``headers`` with a credential header, or a value carrying one of ``secrets``, masked."""
    return {
        name: REDACTED_HEADER
        if name.lower() in CREDENTIAL_HEADERS or any(s and s in value for s in secrets)
        else value
        for name, value in headers.items()
    }


def decode_response_body(body: bytes) -> Any:
    """Decode only after capture, preserving the existing empty-response contract.

    ValueError includes duplicate keys, invalid JSON/encoding and integer limits.
    RecursionError covers excessive nesting. Catch only at the decoder boundary
    so unrelated failures still propagate.
    """
    if not body.strip():
        return None
    try:
        return json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        pass
    # Raise outside the handler: even implicit exception context may hold body
    # bytes or credentials (JSONDecodeError.doc, UnicodeDecodeError.object).
    raise GarminResponseDecodeError()


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

        async def send(attempt: int):
            await self._pace()
            try:
                response, read = await self._send(
                    "POST",
                    f"{self.base_url}{TOKEN_PATH}",
                    {"Authorization": f"ApiKey {key}", "Accept": "*/*"},
                    max_wire_bytes=MAX_TOKEN_BYTES,
                    max_decoded_bytes=MAX_TOKEN_BYTES,
                )
            except httpx.HTTPError as exc:
                event(logger, "GARMIN_TOKEN_FAILED", error_type=type(exc).__name__)
                # Transport exceptions can carry credentials; do not chain them.
                return http_retry.RetryFailure()
            exchanges.append(
                self._capture(
                    _CaptureRequest(TOKEN_KIND, None, "POST", TOKEN_PATH, None, attempt),
                    response,
                    read,
                    key,
                )
            )
            if read.refused:
                raise GarminResponseRefused(TOKEN_PATH, read.refused)
            if http_retry.transient_status(response.status_code):
                return http_retry.RetryFailure(
                    response.status_code, response.headers.get("retry-after")
                )
            token = header_credentials.decode_bearer(read.body)
            if response.status_code != 200 or not token:
                # A rejected credential cannot leave a reusable cache, even when forced.
                # Retain the old bearer only to mask any later response that echoes it.
                self._expires_at = None
                event(logger, "GARMIN_TOKEN_REFUSED", status_code=response.status_code)
                raise GarminAuthError(f"Garmin refused the API key (HTTP {response.status_code})")
            return http_retry.Success(token)

        token = await http_retry.request_with_retry(
            send,
            policy=http_retry.RetryPolicy(
                MAX_ATTEMPTS - 1,
                MAX_RETRY_AFTER,
                backoff,
                lambda header: http_retry.retry_after(header, now=self.clock, strict=False),
            ),
            sleep=self.sleep,
            error=lambda failure, deferred: GarminUnavailableError(
                "Garmin token endpoint temporarily unavailable"
            ),
            notify=lambda failure, attempt, delay: event(
                logger, "GARMIN_TOKEN_RETRY", attempt=attempt, retry_after_seconds=delay
            ),
        )
        # Resolve expiry before publishing either part of the cache; a failed
        # conversion must not pair the new token with the previous expiry.
        expires_at = token_expiry(token, self.clock())
        self._token, self._expires_at = token, expires_at
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

        async def send(attempt: int):
            nonlocal renewed
            # The bearer cache must not bypass key-file validation or rotation.
            # Keep this attempt's key local; captures retain only masked evidence.
            key = read_api_key(self.key_file)
            token = await self.token(exchanges, force=renewed and attempt == 0)
            await self._pace()
            received = await self._get_response(url, token, kind=kind)
            if isinstance(received, http_retry.RetryFailure):
                return received
            response, read = received
            exchanges.append(
                self._capture(
                    _CaptureRequest(kind, context_id, "GET", path, query, attempt),
                    response,
                    read,
                    token,
                    key,
                )
            )
            if read.refused:
                event(logger, "GARMIN_RESPONSE_REFUSED", kind=kind, reason=read.refused)
                raise GarminResponseRefused(path, read.refused)
            status = response.status_code
            if status in (200, 204):
                # 204 (seen live for a day without HRV yet) and an empty 200 body: no data.
                payload = decode_response_body(read.body)
                if exchanges[-1].body_redacted:
                    raise GarminCredentialEchoError()
                return http_retry.Success(GarminResponse(payload, exchanges))
            if status == 401 and not renewed:
                # The token expired or was revoked early: one fresh exchange, then give up.
                renewed = True
                # Invalidate the cache but retain the old token to mask renewal responses.
                self._expires_at = None
                return http_retry.Repeat(reset=True)
            if status == 401:
                raise GarminAuthError("Garmin refused the bearer token twice")
            if http_retry.transient_status(status):
                return http_retry.RetryFailure(status, response.headers.get("retry-after"))
            raise GarminRequestError(status, path)

        return await http_retry.request_with_retry(
            send,
            policy=http_retry.RetryPolicy(
                MAX_ATTEMPTS - 1,
                MAX_RETRY_AFTER,
                backoff,
                lambda header: http_retry.retry_after(header, now=self.clock, strict=False),
                notify_exhausted=True,
            ),
            sleep=self.sleep,
            error=partial(_get_retry_error, path),
            notify=partial(_notify_get_retry, kind),
        )

    async def _get_response(
        self, url: str, token: str, *, kind: str
    ) -> tuple[httpx.Response, ReadBody] | http_retry.RetryFailure:
        """Keep credential-bearing transport errors inside this request boundary.

        HTTP errors may expose the Authorization header both in their message and
        through their request. Retain only the class name for diagnostics, never
        the exception in a retry result, cause or implicit exception context.
        A failed transport produces no capture; responses already received stay
        in the caller's exchanges. Token exchange has its own equivalent boundary.
        """
        try:
            return await self._send(
                "GET",
                url,
                {"Authorization": f"Bearer {token}", "Accept": "application/json"},
                max_wire_bytes=MAX_WIRE_BYTES,
                max_decoded_bytes=MAX_DECODED_BYTES,
            )
        except httpx.HTTPError as exc:
            event(logger, "GARMIN_REQUEST_FAILED", kind=kind, error_type=type(exc).__name__)
        # Leave the handler before returning to retry/raise outside its context.
        return http_retry.RetryFailure()

    async def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        *,
        max_wire_bytes: int,
        max_decoded_bytes: int,
    ) -> tuple[httpx.Response, ReadBody]:
        """One request whose body arrives under the byte budgets (see the module docstring)."""
        request = self.http.build_request(
            method, url, headers={**headers, "Accept-Encoding": ACCEPT_ENCODING}
        )
        response = await self.http.send(request, stream=True)
        read = await read_bounded(
            response, max_wire_bytes=max_wire_bytes, max_decoded_bytes=max_decoded_bytes
        )
        return response, read

    async def _pace(self) -> None:
        now = self.clock()
        if self._last_request_at is not None:
            elapsed = (now - self._last_request_at).total_seconds()
            if elapsed < MIN_INTERVAL:
                await self.sleep(MIN_INTERVAL - elapsed)
        self._last_request_at = self.clock()

    def _capture(
        self,
        request: _CaptureRequest,
        response: httpx.Response,
        read: ReadBody,
        *secrets: str,
    ) -> GarminExchange:
        """``response`` as an exchange; the bearer token in use and ``secrets`` never kept."""
        if self._token:
            secrets = (*secrets, self._token)
        return GarminExchange(
            fetched_at=self.clock(),
            kind=request.kind,
            context_id=request.context_id,
            method=request.method,
            path=request.path,
            query=request.query,
            attempt=request.attempt,
            status_code=response.status_code,
            headers=kept_headers(response.headers),
            body=read.body,
            complete=read.refused is None,
            error=read.refused,
            secrets=secrets,
        )


def _get_retry_error(path: str, failure: http_retry.RetryFailure, deferred: bool) -> Exception:
    if failure.status is None:
        return GarminUnavailableError(f"Garmin unreachable for {path}")
    return GarminUnavailableError(f"Garmin answered HTTP {failure.status} for {path}")


def _notify_get_retry(
    kind: str, failure: http_retry.RetryFailure, attempt: int, delay: float
) -> None:
    if failure.status is not None:
        event(
            logger,
            "GARMIN_RATE_LIMITED" if failure.status == 429 else "GARMIN_SERVER_ERROR",
            kind=kind,
            status_code=failure.status,
            retry_after_seconds=delay,
        )
