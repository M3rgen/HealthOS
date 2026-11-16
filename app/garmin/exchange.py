"""Credential-safe, immutable evidence of Garmin HTTP exchanges."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.garmin import secret_scan

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
class CaptureRequest:
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
    ``metadata_only`` keeps just the digest/size and a fixed body placeholder for
    binary original-file exchanges, avoiding ZIP duplication and compressed secrets.
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
    metadata_only: InitVar[bool] = False
    sha256: str = field(init=False)
    size: int = field(init=False)
    request_sha256: str = field(init=False)
    body_redacted: bool = field(init=False)

    def __post_init__(self, secrets: Sequence[str], metadata_only: bool) -> None:
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
        elif metadata_only:
            object.__setattr__(self, "body", b"[garmin activity file: metadata only]")
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
