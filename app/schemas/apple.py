"""Apple Health responses (GitHub-only extension: app/api/apple.py)."""

from typing import Any, TypedDict


class AppleReceiverResult(TypedDict):
    """``POST /webhooks/apple-health``: one Health Auto Export request body."""

    status: str  # "ok" or "duplicate"
    records: int
    new: int
    rows: int
    skipped: Any
    invalid: Any


class AppleImportRow(TypedDict):
    """One import (upload, CLI import or receiver request); no record content."""

    id: int
    channel: str
    origin: str
    status: str
    filename: str | None
    started_at: str | None
    finished_at: str | None
    bytes_read: int
    records_read: int
    records_new: int
    rows_written: int
    export_date: str | None
    error: str | None
    summary: dict[str, Any]


class AppleReceiverStatus(TypedDict):
    enabled: bool
    token_file_present: bool
    endpoint_configured: bool
    path: str


class AppleStatus(TypedDict):
    """``GET /api/apple/status``: settings, the running import, recent imports and sources."""

    receiver: AppleReceiverStatus
    running: dict[str, Any] | None
    imports: list[dict[str, Any]]
    sources: list[dict[str, Any]] | None
