"""The documented backup-directory override keeps the systemd sandbox and prepares its paths."""

import configparser
import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
from support.backup_scripts import backup_env_file, fake_age, run, sandbox  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/healthos-backup.service"
OVERRIDE = ROOT / "deploy/systemd/healthos-backup-paths.conf.example"


def test_custom_directory_override_resets_both_lists_and_keeps_hardening():
    base = configparser.ConfigParser(strict=False, interpolation=None)
    base.optionxform = str
    base.read(UNIT)
    effective = configparser.ConfigParser(strict=False, interpolation=None)
    effective.optionxform = str
    effective.read([UNIT, OVERRIDE])
    text = OVERRIDE.read_text()
    for name in ("ExecStartPre", "ReadWritePaths"):
        assert f"\n{name}=\n{name}=" in text  # systemd needs explicit list resets.
    service = effective["Service"]
    assert service["ReadWritePaths"].split() == [
        "-/srv/healthos-backups",
        "-/opt/healthos/backup-status",
    ]
    for name, value in base["Service"].items():
        if name not in {"ExecStartPre", "ReadWritePaths"}:
            assert service[name] == value, name
    assert service["ExecStartPre"].startswith("+/bin/sh -ec ")
    assert "/opt/healthos/backups" not in service["ExecStartPre"]


@pytest.mark.parametrize("existing", [False, True])
def test_override_prepares_paths_and_preserves_contents_and_existing_modes(tmp_path, existing):
    config = configparser.ConfigParser(strict=False, interpolation=None)
    config.optionxform = str
    config.read(OVERRIDE)
    command = shlex.split(config["Service"]["ExecStartPre"].removeprefix("+"))
    backups = tmp_path / "new-backups"
    metadata = tmp_path / "metadata"
    old_backups = tmp_path / "old-backups"
    old_backups.mkdir()
    old_dump = old_backups / "health-old.dump.age"
    old_dump.write_bytes(b"synthetic-old-dump")
    if existing:
        backups.mkdir(mode=0o750)
        (backups / "health-existing.dump.age").write_bytes(b"synthetic-copied-dump")
        metadata.mkdir(mode=0o755)
        (metadata / "status.json").write_text('{"result":"success"}')
    calls = tmp_path / "chown-calls"
    chown = tmp_path / "chown"
    chown.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALLS"\n')
    chown.chmod(0o755)
    command[-1] = command[-1].replace("/srv/healthos-backups", shlex.quote(str(backups)))
    command[-1] = command[-1].replace("/opt/healthos/backup-status", shlex.quote(str(metadata)))
    result = subprocess.run(
        command,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(backups.stat().st_mode) == (0o750 if existing else 0o700)
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o755
    assert calls.read_text().splitlines() == ["-Rh", "root:root", str(backups), str(metadata)]
    assert old_dump.read_bytes() == b"synthetic-old-dump"
    if existing:
        assert (backups / "health-existing.dump.age").read_bytes() == b"synthetic-copied-dump"
        assert (metadata / "status.json").read_text() == '{"result":"success"}'


def test_manual_backup_uses_the_same_custom_directory_from_backup_env(fake_age, tmp_path):  # noqa: F811
    custom = tmp_path / "custom-backups"
    custom.mkdir()
    previous = custom / "health-old.dump.age"
    previous.write_bytes(b"synthetic-previous-dump")
    env = {**fake_age}
    del env["BACKUP_HOST_DIR"]
    backup_env_file(env, f"BACKUP_HOST_DIR={custom}\n")
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).parent == custom
    assert previous.read_bytes() == b"synthetic-previous-dump"
    status = json.loads((custom / "last-backup-status.json").read_text())
    assert status["result"] == "success"
    assert Path(status["file"]).parent == custom
    metadata = Path(env["BACKUP_STATUS_HOST_DIR"]) / "status.json"
    assert json.loads(metadata.read_text())["last_attempt"]["result"] == "success"
