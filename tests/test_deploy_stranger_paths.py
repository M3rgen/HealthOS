"""What a stranger following the deployment docs literally depends on (no Docker needed)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from scripts import verify_compose as verifier

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
DEV = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))
# Settings Compose sets itself on the app service.
PINNED = {
    "APP_ENV",
    "APP_HOST",
    "APP_PORT",
    "APP_UI_TOKEN",
    "DATABASE_URL",
    "RECONCILIATION_ENABLED",
    "WATCHDOG_ENABLED",
    "IDENTITY_RECONCILE_ENABLED",
    "BACKUP_STATUS_FILE",
}


@pytest.mark.parametrize("service", ["app", "migrate"])
def test_the_app_container_gets_a_list_of_names_not_the_whole_env_file(service):
    assert "env_file" not in COMPOSE["services"][service]


def test_app_environment_passes_every_setting_the_app_reads():
    environment = COMPOSE["services"]["app"]["environment"]
    wanted = {name.upper() for name in Settings.model_fields} - PINNED
    missing = sorted(wanted - set(environment))
    assert not missing, f"docker-compose.yml app environment omits {missing}"


@pytest.mark.parametrize(
    "name",
    [
        "CLOUDFLARE_TUNNEL_TOKEN",
        "CLOUDFLARED_CREDENTIALS_FILE",
        "CLOUDFLARE_WEBHOOK_HOSTNAME",
        "BACKUP_AGE_RECIPIENT",
        "BACKUP_HOST_DIR",
    ],
)
def test_deployment_only_values_never_reach_the_app_environment(name):
    environment = COMPOSE["services"]["app"]["environment"]
    assert name not in environment
    migrate = COMPOSE["services"]["migrate"]["environment"]
    assert name not in migrate


def test_listed_names_take_their_value_from_env_and_stay_unset_otherwise():
    environment = COMPOSE["services"]["app"]["environment"]
    assert environment["GOOGLE_CLIENT_ID"] is None
    assert environment["HEALTHOS_SOURCES"] is None


def test_dev_override_attaches_postgres_to_a_network_that_can_publish():
    postgres = DEV["services"]["postgres"]
    assert postgres["ports"] == ["127.0.0.1:5432:5432"]
    assert "application" in postgres["networks"]
    assert COMPOSE["networks"]["database"]["internal"] is True
    assert "internal" not in (COMPOSE["networks"].get("application") or {})


def test_dockerignore_excludes_root_only_checkout_state():
    entries = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert {"secrets", "backups", "backup-status", ".env"} <= set(entries)


def test_gitignore_covers_secrets_has_no_duplicates_and_no_private_file_names():
    lines = [
        line
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert "secrets/" in lines
    assert len(lines) == len(set(lines))
    for private in ("VERIFICATION.md", "TODO.md", "*-PLAN*.md"):
        assert private not in lines


@pytest.mark.parametrize(
    "name",
    ["docker-compose.windows.yml", "docker-compose.linux.example.yml", "docker-compose.new.yml"],
)
@pytest.mark.parametrize("services", ["{}", "\n  app:\n    mem_limit: 1g"])
def test_configuration_rejects_empty_compose_overrides(tmp_path, monkeypatch, name, services):
    from scripts import verify_configuration as configuration

    monkeypatch.setattr(configuration, "ROOT", tmp_path)
    assert configuration.check_compose_placeholders() == []
    (tmp_path / name).write_text(f"# Override\nservices: {services}\n", encoding="utf-8")
    errors = configuration.check_compose_placeholders()
    if services == "{}":
        assert errors == [f"{name} is a Compose file containing only services: {{}}"]
    else:
        assert errors == []


def _fake_docker(path: Path, exit_code: int) -> Path:
    script = path / "docker"
    script.write_text(f"#!/usr/bin/env bash\necho probe-output >&2\nexit {exit_code}\n")
    script.chmod(0o755)
    return script


def test_share_probe_fails_clearly_when_the_engine_cannot_see_the_checkout(tmp_path, monkeypatch):
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    monkeypatch.setattr(verifier, "STATUS_DIR", status_dir)
    monkeypatch.setattr(verifier, "DOCKER_COMMAND", str(_fake_docker(tmp_path, 1)))
    with pytest.raises(RuntimeError, match="cannot see this checkout"):
        verifier.check_checkout_is_shared({"PATH": "/usr/bin:/bin"})


def test_share_probe_passes_when_the_bind_mount_is_visible(tmp_path, monkeypatch):
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    monkeypatch.setattr(verifier, "STATUS_DIR", status_dir)
    monkeypatch.setattr(verifier, "DOCKER_COMMAND", str(_fake_docker(tmp_path, 0)))
    verifier.check_checkout_is_shared({"PATH": "/usr/bin:/bin"})
    assert not (status_dir / ".share-probe").exists()


def test_share_probe_uses_the_pinned_postgres_image():
    assert verifier.compose_image("postgres").startswith("postgres:17.10-alpine@sha256:")


def test_compose_model_is_valid_with_the_smoke_values(tmp_path):
    docker = subprocess.run(["which", "docker"], capture_output=True, text=True, check=False)
    if docker.returncode:
        pytest.skip("docker CLI unavailable")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "POSTGRES_PASSWORD": "synthetic-password-for-config-only",
        "APP_UI_TOKEN": "synthetic-token-for-config-only-0000000",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "--quiet"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
