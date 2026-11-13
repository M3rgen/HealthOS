"""IP-8 deployment contracts and real shell startup behavior (no Docker required)."""

import configparser
import json
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.compose_probes import verify_completed_migration
from scripts.verify_configuration import check_compose_runtime

ROOT = Path(__file__).resolve().parents[1]


def test_backup_unit_limits_writes_and_preserves_docker_and_gpg_access():
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.optionxform = str
    unit.read(ROOT / "deploy/systemd/healthos-backup.service")
    service = unit["Service"]
    assert service["User"] == "root"  # Docker socket and the root-owned encryption key.
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "read-only"
    # Optional entries: a missing directory is created by ExecStartPre, not a namespace failure.
    assert service["ReadWritePaths"].split() == [
        "-/opt/healthos/backups",
        "-/opt/healthos/backup-status",
    ]
    assert service["CapabilityBoundingSet"] == service["AmbientCapabilities"] == ""
    # The empty set stays because the deploy user's .env reaches the run through systemd, and
    # only the privileged ExecStartPre takes over the directories that user may have created.
    assert service["LoadCredential"] == "healthos.env:/opt/healthos/.env"
    prepare = service["ExecStartPre"]
    assert prepare.startswith("+")
    assert "chown -Rh root:root /opt/healthos/backups /opt/healthos/backup-status" in prepare
    assert service["RestrictAddressFamilies"].split() == ["AF_UNIX", "AF_INET", "AF_INET6"]
    for option in (
        "NoNewPrivileges",
        "PrivateTmp",
        "PrivateDevices",
        "LockPersonality",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectControlGroups",
        "ProtectClock",
        "ProtectHostname",
        "RestrictNamespaces",
        "RestrictRealtime",
        "RestrictSUIDSGID",
        "MemoryDenyWriteExecute",
    ):
        assert service[option] == "yes", option
    assert service["RuntimeDirectory"] == "healthos-backup-gnupg"
    assert service["RuntimeDirectoryMode"] == "0700"
    assert (
        "Environment=GNUPGHOME=/run/healthos-backup-gnupg"
        in (ROOT / "deploy/systemd/healthos-backup.service").read_text()
    )


@pytest.mark.parametrize("flag", [None, "0", "1", "true"])
def test_entrypoint_migration_is_opt_in_and_execs_the_command(tmp_path, flag):
    (tmp_path / "alembic").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    (tmp_path / "alembic").chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls)}
    env.pop("HEALTHOS_MIGRATE_ON_START", None)
    if flag is not None:
        env["HEALTHOS_MIGRATE_ON_START"] = flag
    with subprocess.Popen(
        [
            "sh",
            str(ROOT / "scripts/docker-entrypoint.sh"),
            "sh",
            "-c",
            'printf "%s\\n" "$$" "$1"; exit 7',
            "--",
            "argument with spaces",
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        output, _ = process.communicate(timeout=120)
        assert process.returncode == 7
        assert output.splitlines() == [str(process.pid), "argument with spaces"]
    assert calls.read_text() == "upgrade head\n" if flag == "1" else not calls.exists()


def test_entrypoint_migration_failure_stops_before_application(tmp_path):
    (tmp_path / "alembic").write_text("#!/bin/sh\nexit 42\n")
    (tmp_path / "alembic").chmod(0o755)
    result = subprocess.run(
        ["sh", str(ROOT / "scripts/docker-entrypoint.sh"), "sh", "-c", "echo app-started"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "HEALTHOS_MIGRATE_ON_START": "1",
        },
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 42 and not result.stdout


def test_image_healthcheck_matches_compose_and_code_stays_root_owned(monkeypatch):
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile.rsplit("FROM ", 1)[1]
    assert "chown" not in runtime and "--chown" not in runtime
    assert "USER healthos" in runtime
    # The account keeps the UID that owns existing volumes, whatever its name.
    assert re.search(
        r"useradd --create-home --uid 10001 --shell /usr/sbin/nologin healthos", runtime
    )
    assert 'ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]' in runtime
    command = json.loads(re.search(r"^CMD (.+)$", runtime, re.M)[1])
    assert "alembic" not in " ".join(command)
    health = re.search(r'HEALTHCHECK (.*?)\s+CMD python -c "([^"]+)"', runtime, re.S)
    assert health
    assert all(
        option in health[1]
        for option in ("--interval=10s", "--timeout=6s", "--start-period=20s", "--retries=12")
    )
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert health[2] == compose["services"]["app"]["healthcheck"]["test"][-1].strip()
    import urllib.request

    calls = []

    def urlopen(url, timeout):
        calls.append((url, timeout))
        if len(calls) == 2:
            raise OSError("unhealthy")
        from io import BytesIO

        return BytesIO(b"ok")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(health[2], {})
    with pytest.raises(OSError, match="unhealthy"):
        exec(health[2], {})
    assert calls == [("http://127.0.0.1:8000/health/live", 5)] * 2


def test_compose_gates_app_on_one_shot_migration_in_production_and_development():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    dev = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text())
    assert check_compose_runtime(base, dev) == []
    migration = base["services"]["migrate"]
    # Migrations run as the schema owner; everything else is the application's environment.
    application = base["services"]["app"]["environment"]
    assert {
        key: value for key, value in migration["environment"].items() if key != "DATABASE_URL"
    } == {key: value for key, value in application.items() if key != "DATABASE_URL"}
    assert "://health:${POSTGRES_PASSWORD:?" in migration["environment"]["DATABASE_URL"]
    assert "${POSTGRES_APP_USER:-health}" in application["DATABASE_URL"]
    assert migration["networks"] == ["database"] and "ports" not in migration
    assert migration["volumes"] == base["services"]["app"]["volumes"]
    broken = deepcopy(base)
    broken["services"]["app"]["depends_on"]["migrate"]["condition"] = "service_started"
    assert "app must depend on successful migrate completion" in check_compose_runtime(broken, dev)
    broken = deepcopy(base)
    broken["services"]["migrate"]["command"] = ["true"]
    assert "migrate must run alembic upgrade head" in check_compose_runtime(broken, dev)


def test_compose_drill_rejects_failed_running_or_implicit_migrations():
    inspection = {
        "State": {"Status": "exited", "ExitCode": 0},
        "Config": {"Cmd": ["alembic", "upgrade", "head"], "Env": ["HEALTHOS_MIGRATE_ON_START=0"]},
    }
    verify_completed_migration(inspection)
    for state in ({"Status": "running", "ExitCode": 0}, {"Status": "exited", "ExitCode": 1}):
        with pytest.raises(RuntimeError, match="did not complete"):
            verify_completed_migration({**inspection, "State": state})
    inspection["Config"]["Env"] = ["HEALTHOS_MIGRATE_ON_START=1"]
    with pytest.raises(RuntimeError, match="entrypoint migration"):
        verify_completed_migration(inspection)
