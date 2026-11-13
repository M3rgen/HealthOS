"""Paths of the Apple Health endpoints and the receiver token, shared by the router, the
middlewares and the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TokenSettings(Protocol):
    """What the receiver token needs of the settings (no import of app.core.config here)."""

    @property
    def apple_receiver_token_file(self) -> Path: ...


# Health Auto Export (iOS) posts here; the only Apple path a non-loopback host may reach.
APPLE_RECEIVER_PATH = "/webhooks/apple-health"
# The local UI uploads export.zip/export.xml here (loopback and the UI token, like /api/*).
APPLE_IMPORT_PATH = "/api/apple/import"
# The receiver token is at least this long (``healthos apple-token`` writes 43 characters).
MIN_TOKEN_LENGTH = 32


def receiver_token(settings: TokenSettings) -> str | None:
    """The receiver token from its file; None when missing, unreadable or too short."""
    try:
        token = Path(settings.apple_receiver_token_file).expanduser().read_text("utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return token if len(token) >= MIN_TOKEN_LENGTH else None
