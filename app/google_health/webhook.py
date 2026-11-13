from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.core.bounded_http import ACCEPT_ENCODING, read_bounded
from app.core.config import GOOGLE_KEYSET_URL_PREFIX
from app.core.response_close import finish_response
from app.core.webhook_types import Reason


class WebhookVerificationError(ValueError):
    def __init__(self, message: str, reason: Reason = "invalid_signature") -> None:
        super().__init__(message)
        self.reason: Reason = reason


@dataclass
class CachedKeyset:
    value: dict
    expires_at: float


# As long as the processor keeps restarting a re-delivered batch (SIGNED_REPLAY_MAX_AGE).
REPLAY_WINDOW_SECONDS = 24 * 3600
REPLAY_WINDOW_MAX_ENTRIES = 4096
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
FORCED_REFRESH_INTERVAL_SECONDS = 60
KEYSET_MAX_BYTES = 64 * 1024


def _canonical_signature(signature: str) -> bytes:
    """Identify equivalent Tink P-256 signatures without changing verification."""
    try:
        signed = base64.b64decode(signature.strip(), validate=True)
        if len(signed) > 5 and signed[0] == 1:
            key_id = signed[1:5]
            r, s = decode_dss_signature(signed[5:])
            s = min(s, P256_ORDER - s)
            return key_id + r.to_bytes(32, "big") + s.to_bytes(32, "big")
    except (ValueError, OverflowError):
        pass
    return signature.strip().encode()


class ReplayWindow:
    """Signatures of deliveries this process already accepted, for a bounded time.

    Google's signature covers only the body and carries no timestamp or nonce, so a copied
    delivery stays verifiable forever; checking a freshness field would break the scheme. ECDSA
    signatures are randomized, so the same signature again is a copy of a delivery, not a new
    one. Within the window such a copy is answered ``204`` before it writes a diagnostic row or
    touches the archive. A delivery is remembered only once its batch was processed without
    failures: Google's retry of a failed request and a
    re-delivery that restarts failed elements still go through. Past the window, or after a
    restart, the payload-hash deduplication and the processor's own age limit apply as before.
    Only a hash of each signature and body is kept; the oldest entry makes room when the window
    is full.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        seconds: float = REPLAY_WINDOW_SECONDS,
        max_entries: int = REPLAY_WINDOW_MAX_ENTRIES,
    ) -> None:
        self.clock = clock
        self.seconds = seconds
        self.max_entries = max_entries
        self.suppressed = 0
        self._expiry: dict[str, float] = {}

    @staticmethod
    def _key(signature: str, body: bytes) -> str:
        # The verified pair: one signature never stands for another body.
        digest = hashlib.sha256(_canonical_signature(signature))
        digest.update(b"\0" + hashlib.sha256(body).digest())
        return digest.hexdigest()

    def seen(self, signature: str, body: bytes) -> bool:
        key = self._key(signature, body)
        expiry = self._expiry.get(key)
        if expiry is None:
            return False
        if expiry <= self.clock():
            del self._expiry[key]
            return False
        self.suppressed += 1
        return True

    def remember(self, signature: str, body: bytes) -> None:
        now = self.clock()
        if len(self._expiry) >= self.max_entries:
            self._expiry = {key: value for key, value in self._expiry.items() if value > now}
        while len(self._expiry) >= self.max_entries:
            del self._expiry[next(iter(self._expiry))]  # insertion order: the oldest first
        self._expiry[self._key(signature, body)] = now + self.seconds


class WebhookSignatureVerifier:
    def __init__(
        self,
        keyset_url: str,
        http: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not keyset_url.startswith(GOOGLE_KEYSET_URL_PREFIX):
            raise ValueError(f"Webhook keyset URL must start with {GOOGLE_KEYSET_URL_PREFIX}")
        self.keyset_url = keyset_url
        self.http = http or httpx.AsyncClient(timeout=10)
        self._cache: CachedKeyset | None = None
        self.monotonic = monotonic
        self._refresh_lock = asyncio.Lock()
        self._last_fetch: float | None = None

    async def verify(self, raw_body: bytes, signature_header: str) -> None:
        try:
            signed = base64.b64decode(signature_header, validate=True)
        except (ValueError, TypeError) as exc:
            raise WebhookVerificationError(
                "Invalid webhook signature encoding", "malformed_signature"
            ) from exc
        if len(signed) <= 5 or signed[0] != 1:
            raise WebhookVerificationError("Invalid Tink signature prefix", "malformed_signature")
        key_id = int.from_bytes(signed[1:5], "big")
        cache = self._cache
        keyset = await self._get_keyset()
        key_entry = _enabled_key(keyset, key_id)
        if key_entry is None and cache is not self._cache:
            # This delivery already downloaded (or shared) the current keyset.
            raise WebhookVerificationError(
                "Official webhook keyset unavailable", "key_download_failed"
            )
        if key_entry is None and self._cache is not None:
            key_entry = _enabled_key(await self._get_keyset(force=True, key_id=key_id), key_id)
        if key_entry is None:
            raise WebhookVerificationError("Webhook signing key is not in the official keyset")
        try:
            serialized = base64.b64decode(key_entry["keyData"]["value"], validate=True)
            fields = _protobuf_fields(serialized)
            x = int.from_bytes(fields[3][0], "big")
            y = int.from_bytes(fields[4][0], "big")
            public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
            public_key.verify(signed[5:], raw_body, ec.ECDSA(hashes.SHA256()))
        except (KeyError, IndexError, ValueError, InvalidSignature) as exc:
            raise WebhookVerificationError("Webhook signature verification failed") from exc

    async def _get_keyset(self, force: bool = False, key_id: int | None = None) -> dict:
        if not force and self._cache and self._cache.expires_at > time.time():
            return self._cache.value
        refresh_in_progress = self._refresh_lock.locked()
        async with self._refresh_lock:
            if self._cache and (
                (not force and self._cache.expires_at > time.time())
                or (force and key_id is not None and _enabled_key(self._cache.value, key_id))
            ):
                return self._cache.value
            # Count failed attempts too; waiters share even a download lasting over a minute.
            if self._last_fetch is not None and (
                refresh_in_progress
                or self.monotonic() - self._last_fetch < FORCED_REFRESH_INTERVAL_SECONDS
            ):
                raise WebhookVerificationError(
                    "Official webhook keyset unavailable", "key_download_failed"
                )
            self._last_fetch = self.monotonic()
            value = await self._download_keyset()
            self._cache = CachedKeyset(value, time.time() + 3600)
            return value

    async def _download_keyset(self) -> dict:
        # No redirects: the keyset must come from the pinned Google host itself.
        try:
            request = self.http.build_request(
                "GET", self.keyset_url, headers={"Accept-Encoding": ACCEPT_ENCODING}
            )
            response = await self.http.send(request, stream=True, follow_redirects=False)
            if not response.is_success:
                failure = await finish_response(response)
                if failure is not None:
                    raise failure
                response.raise_for_status()
            read = await read_bounded(
                response, max_wire_bytes=KEYSET_MAX_BYTES, max_decoded_bytes=KEYSET_MAX_BYTES
            )
            if read.refused:
                raise ValueError("Refused keyset body")
            value = json.loads(read.body)
            if not isinstance(value, dict) or not isinstance(value.get("key"), list):
                raise ValueError("Malformed keyset")
            if any(
                not isinstance(item, dict) or not isinstance(item.get("keyId"), int)
                for item in value["key"]
            ):
                raise ValueError("Malformed key entry")
        except (httpx.HTTPError, ValueError) as exc:
            raise WebhookVerificationError(
                "Official webhook keyset unavailable", "key_download_failed"
            ) from exc
        return value


def _enabled_key(keyset: dict, key_id: int) -> dict | None:
    return next(
        (
            item
            for item in keyset.get("key", [])
            if int(item.get("keyId", -1)) == key_id and item.get("status") == "ENABLED"
        ),
        None,
    )


def _protobuf_fields(data: bytes) -> dict[int, list[bytes]]:
    fields: dict[int, list[bytes]] = {}
    cursor = 0
    while cursor < len(data):
        tag, cursor = _varint(data, cursor)
        field_number, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            _, cursor = _varint(data, cursor)
        elif wire_type == 2:
            size, cursor = _varint(data, cursor)
            end = cursor + size
            if end > len(data):
                raise ValueError("Truncated protobuf field")
            fields.setdefault(field_number, []).append(data[cursor:end])
            cursor = end
        else:
            raise ValueError("Unsupported protobuf wire type")
    return fields


def _varint(data: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while cursor < len(data) and shift < 70:
        byte = data[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if byte < 128:
            return value, cursor
        shift += 7
    raise ValueError("Invalid protobuf varint")
