"""Single-process, single-use OAuth states bound to a browser session cookie.

Only hashes of the cookie are retained; each pending entry also holds its PKCE
verifier, which never enters the signed state or the browser cookie.
A restart invalidates pending flows; callers
must start again, as with expired or pre-HKDF states. No legacy HMAC fallback is safe:
those states have neither a registered nonce nor a browser binding.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable

from app.core.config import Settings
from app.core.oauth_keys import CredentialStoreError, derive_key

COOKIE_NAME = "healthos_oauth_session"
COOKIE_PATH = "/auth/google"
STATE_TTL = 600


class OAuthStateError(ValueError):
    """A malformed, expired or already consumed state."""


class OAuthStateUnavailable(RuntimeError):
    """State issuance is unavailable (configuration or pending capacity)."""


class OAuthStateStore:
    MAX_PENDING = 256

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._pending: dict[str, tuple[str, float, str]] = {}

    def issue(self, settings: Settings, cookie: str, *, code_verifier: str) -> str:
        try:
            key = derive_key(settings.google_token_encryption_key, b"oauth-state-hmac")
        except CredentialStoreError:
            raise OAuthStateUnavailable(
                "A valid Google OAuth encryption key must be configured"
            ) from None
        now = self.clock()
        self._pending = {nonce: entry for nonce, entry in self._pending.items() if entry[1] > now}
        if len(self._pending) >= self.MAX_PENDING:
            raise OAuthStateUnavailable("Too many pending OAuth requests; retry later")
        nonce = secrets.token_urlsafe(24)
        binding = hashlib.sha256(cookie.encode()).hexdigest()
        payload = f"v1.{nonce}.{binding}"
        signature = hmac.digest(key, payload.encode(), "sha256")
        self._pending[nonce] = (binding, now + STATE_TTL, code_verifier)
        return f"{payload}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"

    def consume(self, settings: Settings, state: str, cookie: str | None) -> str:
        try:
            version, nonce, binding, signature = state.split(".")
            key = derive_key(settings.google_token_encryption_key, b"oauth-state-hmac")
            payload = f"{version}.{nonce}.{binding}"
            expected = hmac.digest(key, payload.encode(), "sha256")
            actual = base64.b64decode(
                signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True
            )
            entry = self._pending.get(nonce)
            valid = (
                version == "v1"
                and cookie is not None
                and entry is not None
                and entry[1] > self.clock()
                and hmac.compare_digest(expected, actual)
                and hmac.compare_digest(binding, entry[0])
                and hmac.compare_digest(binding, hashlib.sha256(cookie.encode()).hexdigest())
            )
        except (ValueError, CredentialStoreError):
            raise OAuthStateError("Invalid or expired OAuth state") from None
        if not valid:
            raise OAuthStateError("Invalid or expired OAuth state")
        # No await between checking and consuming: concurrent callbacks cannot both win.
        return self._pending.pop(nonce)[2]
