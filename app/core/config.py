from __future__ import annotations

import ipaddress
import logging
import re
import socket
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlsplit

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.secret_strength import legacy_strong_secret, strong_secret
from app.core.sources import readable_garmin_key, safe_database_url, selected_sources

logger = logging.getLogger(__name__)

# Google's webhook signing keyset is only ever fetched from Google's static host;
# an attacker-controlled keyset URL would let forged webhooks pass verification.
GOOGLE_KEYSET_URL_PREFIX = "https://www.gstatic.com/"


# What APP_ENV may be; anything else, including a stray space, is a startup error instead of a
# silent development mode without the production guard.
APP_ENVIRONMENTS = frozenset({"development", "production", "prod", "test"})
# Bearer access tokens go to this API; production sends them to Google only, over TLS.
GOOGLE_API_HOST_SUFFIX = ".googleapis.com"


def database_password(url: str) -> str:
    try:
        return unquote(urlsplit(url).password or "")
    except ValueError:
        return ""


def google_api_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and host.endswith(GOOGLE_API_HOST_SUFFIX)
            and parsed.username is None
            and parsed.port in {None, 443}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def fernet_key(value: str) -> bool:
    try:
        Fernet(value.encode())
    except (ValueError, TypeError):
        return False
    return True


def safe_bridge_url(value: str) -> bool:
    """Allow local endpoints and the exact Docker host-gateway mapping, never a subnet."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"ws", "wss"}
            or not host
            or parsed.username is not None
            or parsed.fragment
            or parsed.netloc.endswith(":")
            or parsed.port == 0
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            return False
        # Accessing port also rejects malformed and out-of-range port numbers.
        if host in {"localhost", "host.docker.internal"}:
            return True
        address = ipaddress.ip_address(host)
        if address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
            and address.ipv4_mapped.is_loopback
        ):
            return True
        if not address.is_private or address.is_unspecified or address.is_multicast:
            return False
        # Compose maps this name to host-gateway; support its literal IP as well.
        gateways = socket.getaddrinfo("host.docker.internal", None, type=socket.SOCK_STREAM)
        return any(address == ipaddress.ip_address(item[4][0]) for item in gateways)
    except (ValueError, OSError):
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    healthos_sources: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["google"])
    # Keeps a secret that passes only the previous strength rule, with a warning at every start,
    # so an upgrade does not take an installation down before its secrets can be rotated.
    healthos_allow_legacy_secrets: bool = False
    # Production refuses a superuser database login; this date (at most 30 days ahead) lets an
    # install made before POSTGRES_APP_* keep starting until then (app/core/role_policy.py).
    healthos_allow_superuser_until: date | None = None
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://health:health@postgres:5432/health"
    api_read_statement_timeout_seconds: int = Field(default=25, ge=0, le=2_147_483)
    codex_bridge_url: str = "wss://host.docker.internal:8766"
    codex_bridge_token: str | None = None
    # Single-user UI/API bearer token; empty is allowed only on loopback outside production.
    app_ui_token: str | None = None
    app_public_url: str | None = None
    # Enable only behind a proxy that replaces X-Forwarded-Proto (docs/UI_AUTH.md).
    app_trust_proxy_headers: bool = False
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
    google_redirect_uri: str = "http://healthos.localhost:8000/auth/google/callback"
    google_refresh_token: str | None = None
    google_token_encryption_key: str | None = None
    # Retained for configuration compatibility; retired roots cannot activate OAuth tokens.
    google_token_encryption_keys_old: Annotated[list[str], NoDecode] = Field(default_factory=list)
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

    # Apple Health (app/apple, app/api/apple.py). Two ways in, both writing the archive's own
    # Apple tables: an upload of the Health app's export.zip/export.xml from the local UI
    # (always available, behind the UI token like every private route), and a receiver for
    # the Health Auto Export iOS app's REST API automation, off unless enabled. The receiver
    # admits only requests that carry the token kept in APPLE_RECEIVER_TOKEN_FILE; with
    # APPLE_RECEIVER_ENDPOINT_URI set, that https host may reach the receiver path, nothing else.
    apple_receiver_enabled: bool = False
    apple_receiver_token_file: Path = Path("~/.config/healthos/apple-receiver-token")
    apple_receiver_endpoint_uri: str | None = None
    apple_receiver_max_body_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1024 * 1024, le=256 * 1024 * 1024
    )
    apple_receiver_requests_per_minute: int = Field(default=30, ge=1, le=1_000)
    apple_import_max_upload_bytes: int = Field(default=8 * 1024**3, ge=1024 * 1024, le=64 * 1024**3)
    # Most bytes an export.zip may decompress to (zip-bomb guard; a 100:1 ratio guard also holds).
    apple_import_max_uncompressed: int = Field(
        default=16 * 1024**3, ge=1024 * 1024, le=64 * 1024**3
    )

    # Source precedence (app/services/source_precedence.py): when two sources report the same
    # data (same type, overlapping time) the first listed source is shown, the others stay
    # visible as alternatives; nothing is summed. Comma-separated source codes, most preferred
    # first; a source not listed ranks after the listed ones. Reversing a policy is one line.
    source_precedence_activity: str = "GARMIN,FITBIT,HEALTH_KIT"
    source_precedence_health: str = "FITBIT,GARMIN,HEALTH_KIT"

    @field_validator("healthos_sources", mode="before")
    @classmethod
    def normalize_sources(cls, value: str | list[str]) -> list[str]:
        return selected_sources(value)

    @model_validator(mode="after")
    def resolve_legacy_garmin(self) -> Settings:
        # Explicit HEALTHOS_SOURCES is authoritative, including none. Without it, keep
        # the old Google + optional GARMIN_ENABLED behavior.
        if "healthos_sources" not in self.model_fields_set and self.garmin_enabled:
            self.healthos_sources = selected_sources(self.healthos_sources, legacy_garmin=True)
        self.garmin_enabled = "garmin" in self.healthos_sources
        return self

    @property
    def google_enabled(self) -> bool:
        return "google" in self.healthos_sources

    @property
    def production(self) -> bool:
        return self.app_env.casefold() in {"production", "prod"}

    @model_validator(mode="after")
    def require_loopback_without_ui_token(self) -> Settings:
        if not self.app_ui_token and self.app_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(
                "APP_HOST must be 127.0.0.1, ::1 or localhost when APP_UI_TOKEN is missing"
            )
        return self

    @model_validator(mode="after")
    def reject_unsafe_production_configuration(self) -> Settings:
        if not self.production:
            return self

        unsafe: list[str] = []
        required = {"DATABASE_URL": self.database_url, "APP_UI_TOKEN": self.app_ui_token}
        if self.google_enabled:
            required.update(
                {
                    "GOOGLE_CLIENT_ID": self.google_client_id,
                    "GOOGLE_CLIENT_SECRET": self.google_client_secret,
                    "GOOGLE_TOKEN_ENCRYPTION_KEY": self.google_token_encryption_key,
                    "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION": self.google_health_webhook_authorization,
                }
            )
        for name, value in required.items():
            normalized = (value or "").strip()
            if not normalized or normalized.upper() in {"CHANGE_ME", "CHANGEME", "TODO"}:
                unsafe.append(name)
        if not safe_database_url(self.database_url):
            unsafe.append("DATABASE_URL")
        if not safe_bridge_url(self.codex_bridge_url):
            unsafe.append("CODEX_BRIDGE_URL")
        weak = {name: value for name, value in self._secrets().items() if not strong_secret(value)}
        legacy = sorted(name for name, value in weak.items() if legacy_strong_secret(value))
        unsafe += sorted(set(weak) - set(legacy))
        unsafe += self._unsafe_google_settings()
        if self.garmin_enabled and not readable_garmin_key(self.garmin_api_key_file):
            unsafe.append("GARMIN_API_KEY_FILE")
        if legacy and self.healthos_allow_legacy_secrets:
            logger.warning(
                "HEALTHOS_ALLOW_LEGACY_SECRETS: replace these with generated secrets: %s",
                ", ".join(legacy),
            )
        elif legacy:
            unsafe += legacy
        if unsafe:
            names = ", ".join(sorted(set(unsafe)))
            advice = ""
            if legacy and not self.healthos_allow_legacy_secrets:
                advice = (
                    f"; {', '.join(legacy)} passed the previous strength rule: generate a new "
                    "value, or set HEALTHOS_ALLOW_LEGACY_SECRETS=true to keep it for now"
                )
            if "DATABASE_URL" in weak:
                advice += (
                    "; the DATABASE_URL password (under Compose POSTGRES_APP_PASSWORD, or "
                    "POSTGRES_PASSWORD for the owner) must pass the strength check too: change "
                    "it with ALTER ROLE (docs/RUNBOOK.md, Rotate secrets)"
                )
            raise ValueError(f"unsafe or missing production configuration: {names}{advice}")
        return self

    def _secrets(self) -> dict[str, str]:
        """Every production secret held to the generated-secret rule (secret_strength)."""
        secrets_to_check = {"APP_UI_TOKEN": self.app_ui_token or ""}
        if self.google_enabled:
            secrets_to_check["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"] = (
                self.google_health_webhook_authorization
            )
        # An absent key keeps Umay disabled; a configured key must be strong enough.
        if self.codex_bridge_token:
            secrets_to_check["CODEX_BRIDGE_TOKEN"] = self.codex_bridge_token
        # The database password is a secret like the others; a missing one is reported above.
        if password := database_password(self.database_url):
            secrets_to_check["DATABASE_URL"] = password
        return secrets_to_check

    def _unsafe_google_settings(self) -> list[str]:
        unsafe = []
        if not google_api_url(self.google_health_base_url):
            unsafe.append("GOOGLE_HEALTH_BASE_URL")
        if not self.google_enabled:
            return unsafe
        if not (
            self.google_health_webhook_endpoint_uri
            and self.google_health_webhook_endpoint_uri.startswith("https://")
        ):
            unsafe.append("GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI")
        # Checked now rather than at the first OAuth state or token use.
        key = (self.google_token_encryption_key or "").strip()
        if key and key.upper() not in {"CHANGE_ME", "CHANGEME", "TODO"} and not fernet_key(key):
            unsafe.append("GOOGLE_TOKEN_ENCRYPTION_KEY")
        if not all(fernet_key(old) for old in self.google_token_encryption_keys_old):
            unsafe.append("GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD")
        return unsafe

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

    @field_validator("apple_receiver_endpoint_uri", mode="before")
    @classmethod
    def valid_apple_endpoint(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValueError("APPLE_RECEIVER_ENDPOINT_URI must be a URL")
        from urllib.parse import urlsplit

        from app.core.apple_paths import APPLE_RECEIVER_PATH

        parts = urlsplit(value.strip())
        if parts.scheme != "https" or not parts.hostname or parts.path != APPLE_RECEIVER_PATH:
            raise ValueError(
                f"APPLE_RECEIVER_ENDPOINT_URI must be an https URL ending in {APPLE_RECEIVER_PATH}"
            )
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("APPLE_RECEIVER_ENDPOINT_URI takes no query, fragment or user info")
        return value.strip()

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

    @field_validator("app_env", mode="before")
    @classmethod
    def known_app_env(cls, value: object) -> object:
        if (
            not isinstance(value, str)
            or value != value.strip()
            or value.casefold() not in APP_ENVIRONMENTS
        ):
            raise ValueError(
                "APP_ENV must be exactly development, production (or prod) or test; an unknown "
                "value would silently turn off the production guard"
            )
        return value.casefold()

    @field_validator("healthos_allow_superuser_until", mode="before")
    @classmethod
    def empty_date_is_unset(cls, value: object) -> object:
        # Compose passes a name listed in .env without a value as an empty string.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(
        "extra_data_types", "enabled_data_types", "google_token_encryption_keys_old", mode="before"
    )
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
