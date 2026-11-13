"""What a stranger gets by copying .env.example: it boots, and Google stays selectable."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from scripts.verify_configuration import parse_env

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-template-ui-token-with-varied-characters"
DATABASE = "postgresql+asyncpg://health:synthetic-db-Q3v8Lr2Xk9Tw5Zp7Nm4Hc6Jd@postgres:5432/health"


@pytest.fixture
def from_template(monkeypatch):
    """.env.example as the container's environment, with the only two values a stranger sets."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in parse_env(ROOT / ".env.example").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_UI_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", DATABASE)
    return monkeypatch


def test_template_default_boots_in_production_with_no_source(from_template):
    settings = Settings(_env_file=None)
    assert settings.healthos_sources == []
    assert not settings.garmin_enabled
    assert not settings.google_enabled


def test_template_lists_no_legacy_garmin_switch():
    assert "GARMIN_ENABLED" not in parse_env(ROOT / ".env.example")


def test_garmin_only_needs_a_readable_key_file(from_template, tmp_path):
    key = tmp_path / "garmin-key"
    from_template.setenv("HEALTHOS_SOURCES", "garmin")
    from_template.setenv("GARMIN_API_KEY_FILE", str(key))
    with pytest.raises(ValidationError, match="GARMIN_API_KEY_FILE"):
        Settings(_env_file=None)
    key.write_text("synthetic-garmin-key\n", encoding="utf-8")
    settings = Settings(_env_file=None)
    assert settings.healthos_sources == ["garmin"]
    assert settings.garmin_enabled


def test_google_stays_selectable_and_guarded(from_template):
    from_template.setenv("HEALTHOS_SOURCES", "google")
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_ID"):
        Settings(_env_file=None)


def test_an_env_without_the_line_keeps_the_older_behavior(from_template):
    from_template.delenv("HEALTHOS_SOURCES")
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_ID"):
        Settings(_env_file=None)
