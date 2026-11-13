"""Sync, health and provider status responses, shared with their services."""

from datetime import datetime
from typing import Any, NotRequired, TypedDict
from uuid import UUID


class Live(TypedDict):
    status: str
    service: str


class GarminNormalization(TypedDict):
    status: str
    last_attempt_at: str | None
    last_success_at: str | None
    last_error_at: str | None
    last_error_code: str | None


class GarminHealth(TypedDict):
    last_success_at: str | None
    last_error_at: str | None
    last_error_code: str | None
    lag_seconds: int | None
    stale_after_seconds: int
    normalization: GarminNormalization


class PipelineHealth(TypedDict):
    status: str
    database: str
    scheduler: str
    configuration: str
    checked_at: str
    last_webhook_received: str | None
    last_webhook_processed: str | None
    last_sync_success: str | None
    oldest_frequent_sync: str | None
    last_reconciliation: str | None
    latest_health_write: str | None
    data_freshness_checked_at: str | None
    pending_notifications: int
    failed_notifications: int
    failed_syncs: int
    rejected_records: int
    identity_lock_contention: list[dict[str, Any]]
    stale_streams: list[str]
    reasons: list[str]
    data_freshness: dict[str, str | None]
    garmin: NotRequired[GarminHealth]


class Health(PipelineHealth):
    last_backup_success: str | None
    service: str
    active_sources: list[str]
    ui_login_disabled: bool
    watchdog: str


class SyncStatus(TypedDict):
    data_type: str
    status: str
    last_requested_from: datetime | None
    last_requested_to: datetime | None
    last_successful_sync: datetime | None
    records_downloaded: int
    last_error: str | None
    last_run: dict[str, Any] | None


class BackfillStatus(TypedDict):
    data_type: str
    status: str
    windows: int
    records: int
    last_error: str | None


class Accepted(TypedDict):
    status: str
    data_types: NotRequired[list[str]]
    data_type: NotRequired[str]
    sequence: NotRequired[int]
    job_id: NotRequired[str]


class Connected(TypedDict):
    status: str
    health_user_id: str


class ReconciliationType(TypedDict):
    data_type: str
    started_at: datetime | None
    finished_at: datetime | None
    status: str
    inserted: int
    updated: int
    unchanged: int
    source_deleted: int
    pages_committed: int
    error_type: str | None


class ReconciliationRun(TypedDict):
    id: UUID
    requested_from: datetime
    requested_to: datetime
    started_at: datetime
    finished_at: datetime | None
    status: str
    error_type: str | None
    types: list[ReconciliationType]


class ReconciliationHistory(TypedDict):
    runs: list[ReconciliationRun]
    offset: int
    limit: int
    total: int


class Coverage(TypedDict):
    schema_version: str
    start_date: str
    end_date: str
    timezone: str
    types: list[dict[str, Any]]
    supported_data_types: list[str]
    summary: dict[str, int | str]
    receipts_since: str | None
    next_offset: int | None
    note: str


class GarminStatus(TypedDict):
    enabled: bool
    key_file_present: bool
    kinds: dict[str, Any]
    pull_interval_minutes: NotRequired[int]
    lookback_days: NotRequired[int]
    last_success_at: NotRequired[str | None]
    last_error: NotRequired[str | None]
    last_error_at: NotRequired[str | None]
    failing_kinds: NotRequired[list[str]]
    raw_records: NotRequired[int]
    responses: NotRequired[int]
    normalization: NotRequired[GarminNormalization]


# `from` is an existing wire key, not a Python identifier.
GarminDaily = TypedDict(
    "GarminDaily",
    {
        "source": str,
        "from": str,
        "to": str,
        "days": list[dict[str, Any]],
        "precedence": NotRequired[dict[str, str]],
        "policy": NotRequired[dict[str, Any]],
    },
)


class Probe(TypedDict):
    data_type: str
    access_method: str
    accessible: bool
    has_recent_data: bool
    sample_start: NotRequired[datetime | None]
    sample_end: NotRequired[datetime | None]
    top_level_fields: NotRequired[list[str]]
    error_code: NotRequired[str]
    error: NotRequired[str]


class Capabilities(TypedDict):
    lookback_days: int
    data_types: list[Probe]


class GoogleDevices(TypedDict):
    devices: list[dict[str, Any]]


class DeviceReport(TypedDict):
    devices: list[dict[str, Any]]
    # Provider/source/device metadata is open-ended and already encoded by the service.
    google: str
    garmin: str
    apple: str  # GitHub-only Apple extension: "ok" when apple_sources has any, else "off".
    google_unmatched: list[dict[str, Any]]
    note: str
    checked_at: str
    thresholds: dict[str, int]
    precedence: dict[str, Any]


class WebhookStatus(TypedDict):
    checked_at: datetime
    notification_basis: str
    receiver: dict[str, Any]
    subscription: dict[str, Any]
    counters: dict[str, Any]
    last_notification_at: datetime | None
    last_error: dict[str, Any] | None
    silence: dict[str, Any]
    process_local: dict[str, Any]
