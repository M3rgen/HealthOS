"""Domain-separated Google OAuth keys; the configured Fernet key remains the root."""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import Settings


class CredentialStoreError(RuntimeError):
    pass


class GoogleReauthorizationRequired(CredentialStoreError):
    def __init__(self):
        super().__init__(
            "Google bağlantısı için yeniden onay gerekli. "
            "Google bağlantısını yeniden başlatın; çevrimdışı erişim izni verin."
        )


def derive_key(root: str | None, purpose: bytes) -> bytes:
    if not root:
        raise CredentialStoreError("GOOGLE_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        Fernet(root.encode())  # Validate before HKDF, which would accept malformed keys.
        material = base64.urlsafe_b64decode(root)
    except (ValueError, TypeError):
        raise CredentialStoreError(
            "Google OAuth encryption key is not a valid Fernet key"
        ) from None
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"healthos/google-oauth/v1",
        info=purpose,
    ).derive(material)


def token_cipher(settings: Settings) -> Fernet:
    # v1 lazily rewrapped the same live token while keeping old ciphertext forever.
    # A separate domain also forces consent for installations already migrated by v1.
    # Never read retired roots: rotation requires an explicit new offline grant.
    key = derive_key(settings.google_token_encryption_key, b"fernet-reauthorized/v2")
    return Fernet(base64.urlsafe_b64encode(key))
