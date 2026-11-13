"""Shared reason codes for webhook verification and processing diagnostics."""

from enum import StrEnum
from typing import Literal


class NotificationStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    rejected = "rejected"
    interrupted = "interrupted"


# Read compatibility only. New attempts persist the typed interrupted status; diagnostics
# are display text and must never select work in the processor.
INTERRUPTED_ERROR = "Recovered interrupted webhook processing attempt"
_LEGACY_STATUSES: dict[tuple[NotificationStatus, str | None], NotificationStatus] = {
    (NotificationStatus.failed, INTERRUPTED_ERROR): NotificationStatus.interrupted
}


def notification_status(status: NotificationStatus, last_error: str | None) -> NotificationStatus:
    """Decode old failure rows without rewriting their stored status or diagnostics."""
    return _LEGACY_STATUSES.get((status, last_error), status)


Reason = Literal[
    "authorization_rejected",
    "receiver_unconfigured",
    "missing_signature",
    "malformed_signature",
    "invalid_signature",
    "key_download_failed",
    "signature_valid",
    "challenge",
    "invalid_json",
    "body_rejected",
    "notification_received",
    "duplicate",
    "expired_replay",
    "ingestion_failed",
    "notification_rejected",
    "processing_failed",
    "batch_processing_failed",
    "processing_interrupted",
]
