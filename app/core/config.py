from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Google's webhook signing keyset is only ever fetched from Google's static host;
# an attacker-controlled keyset URL would let forged webhooks pass verification.
GOOGLE_KEYSET_URL_PREFIX = "https://www.gstatic.com/"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://health:health@postgres:5432/health"
    codex_bridge_url: str = "ws://host.docker.internal:8766"
    codex_bridge_token: str | None = None
    # Single-user UI/API bearer token; empty disables the barrier outside production.
    app_ui_token: str | None = None
    backup_status_file: Path | None = None
    manual_write_max_body_bytes: int = Field(
        default=4096,
        ge=1024,
        le=64 * 1024,
        description="Byte limit shared by manual measurement, workout and flag POST bodies.",
    )
    manual_write_requests_per_minute: int = Field(
        default=60,
        ge=1,
        le=10_000,
        description="Application-wide manual write budget per fixed 60-second window.",
    )
    webhook_requests_per_minute: int = Field(
        default=120,
        ge=1,
        le=10_000,
        description=(
            "Capacity of each webhook budget: authorized requests (challenges included) and "
            "requests without the configured authorization are counted separately."
        ),
    )

    google_health_base_url: str = "https://health.googleapis.com/v4"
    google_client_id: str = "CHANGE_ME"
    google_client_secret: str = "CHANGE_ME"
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_refresh_token: str | None = None
    google_token_encryption_key: str | None = None
    google_cloud_project_number: str | None = None
    google_health_webhook_endpoint_uri: str | None = None
    google_health_webhook_subscriber_id: str = "personal-health-mirror"
    google_health_webhook_authorization: str = "CHANGE_ME"
    google_health_webhook_max_body_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024
    )
    google_health_webhook_public_key_url: str = (
        "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"
    )

    backfill_start_date: date = date(2010, 1, 1)
    backfill_window_days: int = Field(default=31, ge=1, le=366)
    google_api_page_size: int = Field(default=10_000, ge=1, le=10_000)
    google_api_max_retries: int = Field(default=6, ge=0, le=12)
    google_api_min_request_interval_seconds: float = Field(default=0.25, ge=0, le=10)
    sync_concurrency: int = Field(default=2, ge=1, le=10)
    extra_data_types: Annotated[list[str], NoDecode] = Field(default_factory=list)
    enabled_data_types: Annotated[list[str], NoDecode] = Field(default_factory=list)

    reconciliation_enabled: bool = True
    reconciliation_daily_hour_utc: int = Field(default=2, ge=0, le=23)
    reconciliation_weekly_day: str = "sun"
    reconciliation_weekly_hour_utc: int = Field(default=3, ge=0, le=23)
    reconciliation_daily_days: int = Field(default=7, ge=1)
    reconciliation_weekly_days: int = Field(default=90, ge=1)
    # The nightly identity reconciler (identity-backfill --incremental): decides source
    # chains and links duplicate rows. It starts when the scheduled Google reconciliations
    # have ended; at the hour and minute below only when RECONCILIATION_ENABLED is false. A data
    # type whose lock stays busy (any long sync) is retried until the UNTIL time (end of the
    # nightly window); after it, and by hand, for LOCK_WAIT_MINUTES (0: 8 attempts only).
    identity_reconcile_enabled: bool = True
    identity_reconcile_hour_utc: int = Field(default=2, ge=0, le=23)
    identity_reconcile_minute: int = Field(default=30, ge=0, le=59)
    identity_reconcile_until_hour_utc: int = Field(default=3, ge=0, le=23)
    identity_reconcile_until_minute: int = Field(default=55, ge=0, le=59)
    identity_reconcile_lock_wait_minutes: int = Field(default=60, ge=0, le=720)

    watchdog_enabled: bool = True
    watchdog_interval_seconds: int = Field(default=900, ge=60, le=86_400)
    pipeline_stale_threshold_minutes: int = Field(default=120, ge=15, le=10_080)
    # raw_health_data freshness summary (data_freshness, latest_health_write) is
    # recomputed at most this often; /health serves the cached result in between.
    pipeline_freshness_refresh_seconds: int = Field(default=3_600, ge=60, le=86_400)
    recovery_cooldown_minutes: int = Field(default=360, ge=15, le=43_200)
    startup_gap_threshold_minutes: int = Field(default=120, ge=15, le=10_080)
    automatic_recovery_enabled: bool = True
    automatic_recovery_max_days: int = Field(default=7, ge=1, le=30)
    # Cihazlarım: a device whose newest record is at most this old is "bağlı"; older than
    # devices_delayed_days it is "kopuk"; "gecikmeli" in between.
    devices_connected_hours: int = Field(default=48, ge=1, le=24 * 365)
    devices_delayed_days: int = Field(default=7, ge=1, le=3650)

    # Garmin Connect Personal API (app/garmin, app/services/garmin_pull.py): read-only,
    # outbound-only pulls into the archive's own Garmin tables. Off unless enabled; the
    # Personal API key is read from a file, never from the environment or the database.
    garmin_enabled: bool = False
    garmin_api_key_file: Path = Path("~/.config/garmin/connect-personal-api-key")
    garmin_pull_interval_minutes: int = Field(default=120, ge=15, le=1440)
    garmin_lookback_days: int = Field(default=3, ge=1, le=28)
    # What the Garmin source is called in Cihazlarım (the Personal API exposes no device model).
    garmin_device_name: str = Field(default="Garmin", min_length=1, max_length=120)

    # Source precedence (app/services/source_precedence.py): when two sources report the same
    # data (same type, overlapping time) the first listed source is shown, the others stay
    # visible as alternatives; nothing is summed. Comma-separated source codes, most preferred
    # first; a source not listed ranks after the listed ones. Reversing a policy is one line.
    source_precedence_activity: str = "GARMIN,FITBIT,HEALTH_KIT"
    source_precedence_health: str = "FITBIT,GARMIN,HEALTH_KIT"

    @property
    def production(self) -> bool:
        return self.app_env.casefold() in {"production", "prod"}

    @model_validator(mode="after")
    def reject_unsafe_production_configuration(self) -> Settings:
        if not self.production:
            return self

        unsafe: list[str] = []
        required = {
            "DATABASE_URL": self.database_url,
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "GOOGLE_TOKEN_ENCRYPTION_KEY": self.google_token_encryption_key,
            "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION": self.google_health_webhook_authorization,
            "APP_UI_TOKEN": self.app_ui_token,
        }
        for name, value in required.items():
            normalized = (value or "").strip()
            if not normalized or normalized.upper() in {"CHANGE_ME", "CHANGEME", "TODO"}:
                unsafe.append(name)
        if self.database_url == "postgresql+asyncpg://health:health@postgres:5432/health":
            unsafe.append("DATABASE_URL")
        if not (
            self.google_health_webhook_endpoint_uri
            and self.google_health_webhook_endpoint_uri.startswith("https://")
        ):
            unsafe.append("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI")
        if unsafe:
            names = ", ".join(sorted(set(unsafe)))
            raise ValueError(f"unsafe or missing production configuration: {names}")
        return self

    @field_validator("source_precedence_activity", "source_precedence_health")
    @classmethod
    def normalize_precedence(cls, value: str) -> str:
        codes = [code.strip().upper() for code in value.split(",") if code.strip()]
        if not codes:
            raise ValueError("a source precedence lists at least one source code")
        if len(set(codes)) != len(codes):
            raise ValueError("a source precedence lists each source code once")
        if any(not code.replace("_", "").isalnum() for code in codes):
            raise ValueError("source codes are letters, digits and underscores (FITBIT, GARMIN)")
        return ",".join(codes)

    @field_validator("app_ui_token", mode="before")
    @classmethod
    def valid_ui_token(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{32,512}", value):
            raise ValueError(
                "APP_UI_TOKEN must be 32-512 printable ASCII characters without spaces; "
                'generate one with: python -c "import secrets;print(secrets.token_urlsafe(32))"'
            )
        return value

    @field_validator("extra_data_types", "enabled_data_types", mode="before")
    @classmethod
    def parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("google_health_webhook_public_key_url")
    @classmethod
    def require_google_keyset_host(cls, value: str) -> str:
        if not value.startswith(GOOGLE_KEYSET_URL_PREFIX):
            raise ValueError(
                f"webhook public keyset URL must start with {GOOGLE_KEYSET_URL_PREFIX}"
            )
        return value

    @property
    def credentials_configured(self) -> bool:
        return all(
            value and value != "CHANGE_ME"
            for value in (self.google_client_id, self.google_client_secret)
        )

    @property
    def webhook_readiness(self) -> dict[str, bool]:
        authorization = self.google_health_webhook_authorization.strip()
        result = {
            "project_number_configured": bool(
                self.google_cloud_project_number and self.google_cloud_project_number.isdigit()
            ),
            "https_endpoint_configured": bool(
                self.google_health_webhook_endpoint_uri
                and self.google_health_webhook_endpoint_uri.startswith("https://")
            ),
            "authorization_configured": bool(
                authorization != "CHANGE_ME" and authorization.startswith(("Bearer ", "Basic "))
            ),
            "subscriber_id_valid": bool(
                re.fullmatch(
                    r"[a-z]([a-z0-9-]{2,34}[a-z0-9])",
                    self.google_health_webhook_subscriber_id,
                )
            ),
        }
        result["external_registration_ready"] = all(result.values())
        return result

    def safety_window(self, data_type: str) -> timedelta:
        if data_type == "heart-rate":
            return timedelta(minutes=60)
        if data_type in {"sleep", "daily-sleep-temperature-derivations"}:
            return timedelta(hours=72)
        if data_type.startswith("daily-"):
            return timedelta(days=7)
        if data_type in {"steps", "distance", "active-energy-burned", "active-zone-minutes"}:
            return timedelta(hours=12)
        return timedelta(hours=24)


@lru_cache
def get_settings() -> Settings:
    return Settings()
