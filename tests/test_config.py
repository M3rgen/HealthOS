import pytest
from pydantic import ValidationError
from support.fixture_keys import SYNTHETIC_FERNET_KEY

from app.core.config import Settings


def test_empty_or_placeholder_oauth_credentials_are_not_configured():
    assert not Settings(google_client_id="", google_client_secret="").credentials_configured
    assert not Settings(
        google_client_id="CHANGE_ME", google_client_secret="secret"
    ).credentials_configured
    assert not Settings(
        google_client_id="client", google_client_secret="CHANGE_ME"
    ).credentials_configured


def test_nonempty_oauth_credentials_are_configured():
    assert Settings(google_client_id="client", google_client_secret="secret").credentials_configured


def test_webhook_readiness_requires_public_registration_inputs():
    readiness = Settings(_env_file=None).webhook_readiness
    assert not readiness["external_registration_ready"]
    assert readiness["subscriber_id_valid"]


def test_webhook_readiness_accepts_complete_safe_configuration():
    readiness = Settings(
        google_cloud_project_number="1234567890",
        google_health_webhook_endpoint_uri="https://health.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer local-secret",
    ).webhook_readiness
    assert all(readiness.values())


def test_production_rejects_missing_and_placeholder_secrets():
    with pytest.raises(ValidationError, match="unsafe or missing production configuration"):
        Settings(app_env="production", _env_file=None)


def test_production_accepts_explicit_non_placeholder_secrets():
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health",
        google_client_id="client-id",
        google_client_secret="client-secret",
        google_token_encryption_key=SYNTHETIC_FERNET_KEY,
        google_health_webhook_endpoint_uri="https://health.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer webhook-secret-0123456789-abcdefgh",
        app_ui_token="synthetic-ui-token-0123456789abcdef",
        _env_file=None,
    )
    assert settings.app_env == "production"


def production(**overrides):
    return Settings(
        _env_file=None,
        **(
            {
                "app_env": "production",
                "healthos_sources": "none",
                "database_url": "postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres/health",
                "app_ui_token": "synthetic-ui-token-0123456789abcdef",
            }
            | overrides
        ),
    )


@pytest.mark.parametrize("environment", ["production", "PROD"])
@pytest.mark.parametrize(
    "url",
    [
        "ws://192.0.2.10:8766",
        "wss://remote.example:8766",
        "ws://10.0.0.1:8766",
        "ws://0.0.0.0:8766",
        "ws://[::]:8766",
        "ws://127.1:8766",
        "ws://2130706433:8766",
        "ws://host.docker.internal.evil.example:8766",
        "ws://host.docker.internal@remote.example:8766",
        "ws://user:password@localhost:8766",
        "http://localhost:8766",
        "ws://localhost:65536",
        "ws://[::1",
        "ws://localhost:8766/#fragment",
    ],
)
def test_production_rejects_nonlocal_or_invalid_bridge(url, environment, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    with pytest.raises(ValidationError, match="CODEX_BRIDGE_URL") as failure:
        production(app_env=environment, codex_bridge_url=url)
    assert url not in str(failure.value)


@pytest.mark.parametrize(
    "url",
    [
        "ws://host.docker.internal:8766",
        "ws://localhost:8766",
        "wss://127.0.0.1:8766",
        "ws://127.0.0.2:8766",
        "ws://[::1]:8766",
        "ws://[::ffff:127.0.0.1]:8766",
        "ws://172.19.0.1:8766",
    ],
)
def test_production_accepts_local_bridge_and_exact_docker_gateway(url, monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("172.19.0.1", 0))]
    )
    assert production(codex_bridge_url=url).codex_bridge_url == url


def test_bridge_gateway_lookup_failure_rejects_numeric_address(monkeypatch):
    import socket

    def unavailable(*args, **kwargs):
        raise socket.gaierror("synthetic resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    with pytest.raises(ValidationError, match="CODEX_BRIDGE_URL"):
        production(codex_bridge_url="ws://172.19.0.1:8766")


def test_bridge_does_not_accept_other_addresses_on_the_docker_subnet(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("172.19.0.1", 0))]
    )
    with pytest.raises(ValidationError, match="CODEX_BRIDGE_URL"):
        production(codex_bridge_url="ws://172.19.0.2:8766")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("codex_bridge_token", "x"),
        ("codex_bridge_token", "short-123456789"),
        ("codex_bridge_token", "x" * 32),
        ("google_health_webhook_authorization", "short-123456789"),
        ("google_health_webhook_authorization", "x" * 32),
        ("google_health_webhook_authorization", "Bearer " + "x" * 32),
        ("google_health_webhook_authorization", "Basic " + "x" * 32),
        ("app_ui_token", "x" * 32),
        ("app_ui_token", "short-123456789"),
    ],
)
def test_production_rejects_weak_secrets_without_exposing_values(field, value):
    with pytest.raises(ValidationError, match=field.upper()) as failure:
        production(
            healthos_sources="google",
            google_client_id="synthetic-client",
            google_client_secret="synthetic-client-secret",
            google_token_encryption_key=SYNTHETIC_FERNET_KEY,
            google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
            **{field: value},
        )
    message = str(failure.value)
    assert value not in message and "synthetic-client-secret" not in message


def production_with_webhook(value, **overrides):
    return production(
        healthos_sources="google",
        google_client_id="synthetic-client",
        google_client_secret="synthetic-client-secret",
        google_token_encryption_key=SYNTHETIC_FERNET_KEY,
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization=value,
        **overrides,
    )


@pytest.mark.parametrize(
    "value",
    [
        "synthetic-webhook-authorization-0123456789",
        "Bearer webhook-secret-0123456789-abcdefgh",
        "Basic basic-secret-0a1b2c3d4e5f6g7h8i9j-kLmNzx",
    ],
)
def test_valid_webhook_authorization_is_preserved(value):
    assert production_with_webhook(value).google_health_webhook_authorization == value


@pytest.mark.parametrize(
    "value",
    ["synthetic-123456", "Bearer webhook-secret-0123456789", "Basic basic-secret-0123456789"],
)
def test_an_older_webhook_authorization_is_kept_only_with_the_option(value):
    with pytest.raises(ValueError, match="HEALTHOS_ALLOW_LEGACY_SECRETS"):
        production_with_webhook(value)
    kept = production_with_webhook(value, healthos_allow_legacy_secrets=True)
    assert kept.google_health_webhook_authorization == value


def test_optional_bridge_and_development_configuration_still_work():
    assert production(codex_bridge_token=None).codex_bridge_token is None
    token = "synthetic-bridge-token-0123456789-abcdef"
    assert production(codex_bridge_token=token).codex_bridge_token == token
    legacy = "synthetic-123456"  # 16 characters, the previous minimum
    kept = production(codex_bridge_token=legacy, healthos_allow_legacy_secrets=True)
    assert kept.codex_bridge_token == legacy
    settings = Settings(
        _env_file=None,
        codex_bridge_url="ws://192.0.2.10:8766",
        codex_bridge_token="x",
        app_ui_token="x" * 32,
    )
    assert settings.codex_bridge_token == "x"


def test_garmin_is_off_by_default_and_its_bounds_hold():
    settings = Settings(_env_file=None)
    assert settings.garmin_enabled is False
    assert str(settings.garmin_api_key_file) == "~/.config/garmin/connect-personal-api-key"
    assert settings.garmin_pull_interval_minutes == 120
    assert settings.garmin_lookback_days == 3
    with pytest.raises(ValidationError):
        Settings(_env_file=None, garmin_pull_interval_minutes=5)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, garmin_lookback_days=29)
    custom = Settings(
        _env_file=None,
        garmin_enabled=True,
        garmin_api_key_file="/run/secrets/garmin-key",
        garmin_lookback_days=7,
    )
    assert (
        custom.garmin_enabled and custom.garmin_api_key_file.as_posix() == "/run/secrets/garmin-key"
    )
