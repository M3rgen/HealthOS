"""Production refuses weak database passwords, a non-Google API URL, a malformed Fernet key and
an unknown APP_ENV (synthetic values only)."""

from __future__ import annotations

import secrets

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings

STRONG_DB = secrets.token_hex(32)


def production(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "healthos_sources": "google",
        "database_url": f"postgresql+asyncpg://healthos_app:{STRONG_DB}@postgres:5432/health",
        "app_ui_token": secrets.token_urlsafe(32),
        "google_client_id": "synthetic-client",
        "google_client_secret": "synthetic-secret",
        "google_token_encryption_key": Fernet.generate_key().decode(),
        "google_health_webhook_endpoint_uri": "https://hooks.example/webhooks/google-health",
        "google_health_webhook_authorization": f"Bearer {secrets.token_urlsafe(32)}",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def refused(**overrides) -> str:
    with pytest.raises(ValidationError) as error:
        production(**overrides)
    return str(error.value)


def test_a_complete_configuration_starts():
    assert production().production


@pytest.mark.parametrize("password", ["abc", "Hunter2", "a" * 40, "0123456789abcdefghij0123"])
def test_weak_database_password_is_refused_and_named(password):
    message = refused(database_url=f"postgresql+asyncpg://healthos_app:{password}@postgres/health")
    assert "DATABASE_URL" in message
    assert "POSTGRES_APP_PASSWORD" in message and "POSTGRES_PASSWORD" in message
    assert password not in message  # the value never enters the error


def test_legacy_strength_database_password_needs_the_explicit_aid():
    legacy = "q7Zr-Lm2x-Vb9k-Tn4w"  # passes only the earlier 16-character rule
    url = f"postgresql+asyncpg://healthos_app:{legacy}@postgres/health"
    assert "HEALTHOS_ALLOW_LEGACY_SECRETS" in refused(database_url=url)
    assert production(database_url=url, healthos_allow_legacy_secrets=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://health.googleapis.com/v4",
        "https://health.example.com/v4",
        "https://googleapis.com.attacker.example/v4",
        "https://user@health.googleapis.com/v4",
        "https://health.googleapis.com:8443/v4",
        "https://health.googleapis.com/v4?x=1",
        "not a url",
    ],
)
def test_google_api_url_must_be_https_on_a_google_host(url):
    assert "GOOGLE_HEALTH_BASE_URL" in refused(google_health_base_url=url)


def test_google_api_url_is_checked_without_google_too():
    assert "GOOGLE_HEALTH_BASE_URL" in refused(
        healthos_sources="none", google_health_base_url="http://health.googleapis.com/v4"
    )


def test_default_google_api_url_passes():
    assert production(google_health_base_url="https://health.googleapis.com/v4")


@pytest.mark.parametrize(
    "key", ["not-a-fernet-key", "x" * 44, "c3ludGhldGljLWZlcm5ldC1rZXktMzItYnl0ZXMtIS"]
)
def test_malformed_token_key_is_refused_at_startup(key):
    message = refused(google_token_encryption_key=key)
    assert "GOOGLE_TOKEN_ENCRYPTION_KEY" in message and key not in message


def test_malformed_old_token_key_is_refused_at_startup():
    old = f"{Fernet.generate_key().decode()},broken"
    assert "GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD" in refused(google_token_encryption_keys_old=old)


@pytest.mark.parametrize(
    "value", ["production ", " production", "staging", "dev", "", "PRODUCTION\n", "prod\t"]
)
def test_unknown_or_padded_app_env_is_refused(value):
    with pytest.raises(ValidationError, match="APP_ENV"):
        Settings(_env_file=None, app_env=value)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("production", "production"),
        ("Production", "production"),
        ("prod", "prod"),
        ("development", "development"),
        ("test", "test"),
    ],
)
def test_known_app_env_values_are_kept(value, expected):
    production_like = expected in {"production", "prod"}
    settings = (
        production(app_env=value) if production_like else Settings(_env_file=None, app_env=value)
    )
    assert settings.app_env == expected


def test_padded_production_no_longer_skips_the_guard():
    """`APP_ENV="production "` used to mean development: the weak token below then passed."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production ", app_ui_token="a" * 32)
