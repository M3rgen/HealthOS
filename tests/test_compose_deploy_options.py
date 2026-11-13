"""Compose options that have real behaviour attached: ports, Garmin, roles, limits, backups."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest
import yaml

from scripts.verify_configuration import check_environment

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


def _environment(service: str) -> dict:
    return SERVICES[service]["environment"]


def test_app_port_in_env_file_only_moves_the_published_port():
    # An APP_PORT in .env moves only the published host port; pinning it keeps uvicorn on
    # the port the mapping and healthcheck use (it was the regression the smoke test hid).
    assert _environment("app")["APP_PORT"] == "8000"
    assert SERVICES["app"]["ports"] == ["127.0.0.1:${APP_PORT:-8000}:8000"]


def test_smoke_env_file_really_contains_a_non_default_app_port(tmp_path, monkeypatch):
    from scripts import verify_compose as verifier

    monkeypatch.setattr(verifier, "SMOKE_ENV_FILE", tmp_path / "env")
    verifier.write_smoke_env_file()
    lines = (tmp_path / "env").read_text("utf-8").splitlines()
    assert verifier.HOST_PORT != "8000"
    assert f"APP_PORT={verifier.HOST_PORT}" in lines
    assert "APP_PORT=8000" not in lines


def test_verifier_fails_when_compose_stops_pinning_the_container_port():
    from scripts.verify_configuration import check_compose_runtime

    dev = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    broken = yaml.safe_load(yaml.safe_dump(COMPOSE))
    del broken["services"]["app"]["environment"]["APP_PORT"]
    assert 'app must pin APP_PORT: "8000" inside the container' in check_compose_runtime(
        broken, dev
    )


def test_garmin_is_enabled_by_an_override_file_not_by_editing_a_tracked_file():
    base = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "GARMIN_API_KEY_HOST_FILE" not in base and "garmin-personal-api-key" not in base
    override = yaml.safe_load((ROOT / "docker-compose.garmin.yml").read_text(encoding="utf-8"))
    services = override["services"]
    assert set(services) == {"app", "migrate"}
    for service in services.values():
        assert service["environment"] == {
            "GARMIN_API_KEY_FILE": "/run/secrets/garmin-personal-api-key"
        }
        assert service["volumes"] == [
            {
                "type": "bind",
                "source": "${GARMIN_API_KEY_HOST_FILE:?Set GARMIN_API_KEY_HOST_FILE to the host "
                "path of the Garmin key file}",
                "target": "/run/secrets/garmin-personal-api-key",
                "read_only": True,
                # A mistyped path fails `up` and names it; Docker must not create it as root.
                "bind": {"create_host_path": False},
            }
        ]


def test_empty_compatibility_overrides_are_removed():
    for name in ("docker-compose.windows.yml", "docker-compose.linux.example.yml"):
        assert not (ROOT / name).exists()


def test_app_connects_with_its_own_role_when_configured_and_the_owner_otherwise():
    app_url = _environment("app")["DATABASE_URL"]
    assert app_url.startswith(
        "postgresql+asyncpg://${POSTGRES_APP_USER:-health}:"
        "${POSTGRES_APP_PASSWORD:-${POSTGRES_PASSWORD:?"
    )
    assert _environment("migrate")["DATABASE_URL"].startswith(
        "postgresql+asyncpg://health:${POSTGRES_PASSWORD:?"
    )
    assert _environment("provision-app-role")["DATABASE_URL"].startswith(
        "postgresql+asyncpg://health:${POSTGRES_PASSWORD:?"
    )
    # The owner password must not reach the application process through env_file.
    assert _environment("app")["POSTGRES_PASSWORD"] == ""
    # The role provisioner sees the password; the app's own URL is all it needs afterwards.
    assert set(_environment("provision-app-role")) == {
        "DATABASE_URL",
        "POSTGRES_APP_USER",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_ALLOW_OWNER_RUNTIME",
        # The app password meets the shared strength rule, with the same upgrade aid.
        "HEALTHOS_ALLOW_LEGACY_SECRETS",
    }
    assert "env_file" not in SERVICES["provision-app-role"]
    assert "volumes" not in SERVICES["provision-app-role"]


def test_every_long_running_and_one_shot_service_has_resource_limits():
    for name in ("postgres", "app", "migrate", "provision-app-role", "backup", "caddy"):
        assert SERVICES[name]["mem_limit"], name
        assert SERVICES[name]["pids_limit"], name


def test_caddy_is_hardened_like_the_other_services():
    caddy = SERVICES["caddy"]
    assert caddy["read_only"] is True
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["cap_add"] == ["NET_BIND_SERVICE"]
    assert caddy["security_opt"] == ["no-new-privileges:true"]
    assert caddy["tmpfs"] == ["/tmp"]


def test_backup_unit_creates_its_directories_before_the_sandbox_needs_them():
    text = (ROOT / "deploy" / "systemd" / "healthos-backup.service").read_text(encoding="utf-8")
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(text)
    service = parser["Service"]
    paths = [item.lstrip("-") for item in service["ReadWritePaths"].split()]
    assert paths == ["/opt/healthos/backups", "/opt/healthos/backup-status"]
    assert all(item.startswith("-") for item in service["ReadWritePaths"].split())
    pre = service["ExecStartPre"]
    assert pre.startswith("+/bin/sh ")
    for path in paths:
        assert f"test -d {path} || install -d" in pre


def test_failure_notifier_is_sandboxed_without_blocking_the_journal():
    text = (ROOT / "deploy" / "systemd" / "healthos-backup-failure.service").read_text("utf-8")
    assert "PrivateNetwork=yes" in text and "ProtectSystem=strict" in text
    assert "/usr/bin/logger --priority user.crit" in text
    assert "RestrictAddressFamilies=AF_UNIX" in text


def _production_env(tmp_path, extra: str) -> list[str]:
    path = tmp_path / "env"
    path.write_text(
        "HEALTHOS_SOURCES=none\nAPP_UI_TOKEN="
        + "t" * 32
        + "\nPOSTGRES_PASSWORD="
        + "p" * 32
        + "\nDATABASE_URL=postgresql+asyncpg://health:"
        + "p" * 32
        + "@db/health\n"
        + extra,
        encoding="utf-8",
    )
    return check_environment(path)


@pytest.mark.parametrize(
    ("extra", "problem"),
    [
        ("POSTGRES_APP_USER=healthos_app\n", "set both"),
        ("POSTGRES_APP_PASSWORD=" + "a" * 32 + "\n", "set both"),
        ("POSTGRES_APP_USER=healthos_app\nPOSTGRES_APP_PASSWORD=short\n", "URL-safe"),
    ],
)
def test_half_configured_application_role_is_reported(tmp_path, extra, problem):
    errors = _production_env(tmp_path, extra)
    assert len(errors) == 1 and problem in errors[0]


def test_owner_only_env_file_and_complete_role_both_validate(tmp_path):
    assert _production_env(tmp_path, "") == []
    complete = "POSTGRES_APP_USER=healthos_app\nPOSTGRES_APP_PASSWORD=" + "a" * 64 + "\n"
    assert _production_env(tmp_path, complete) == []
