"""Host Python failures must replace stale public backup evidence without leaking paths."""

import json
import shutil
import stat
import sys
from pathlib import Path

import pytest
from support.backup_scripts import FAKE_DOCKER, run, status
from test_health_backup_reason import report_for

from app.services.operations.backup_status import backup_reasons, read_backup_status


def executable(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def shell_env(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # An explicit PATH proves the absent-python case cannot find a system Python.
    for name in (
        "bash",
        "dirname",
        "sed",
        "tr",
        "grep",
        "id",
        "date",
        "mkdir",
        "df",
        "awk",
        "mktemp",
        "chmod",
        "mv",
        "rm",
        "wc",
        "cat",
    ):
        (binaries / name).symlink_to(shutil.which(name))
    (binaries / "python3").symlink_to(sys.executable)
    executable(binaries / "docker", FAKE_DOCKER.split("\n", 1)[1])
    executable(binaries / "age", 'printf "encrypted-header\\n"\ncat\n')
    return {
        "PATH": str(binaries),
        # The installer extension's backup.sh also tries other Pythons: only this PATH's python3.
        "BACKUP_PYTHON": "python3",
        "BACKUP_HOST_DIR": str(tmp_path / "backups"),
        "BACKUP_STATUS_HOST_DIR": str(tmp_path / "metadata"),
        "BACKUP_ENV_FILE": str(tmp_path / "absent.env"),
        "BACKUP_AGE_RECIPIENT": "synthetic-public-recipient",
        "BACKUP_MIN_FREE_MB": "0",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
    }


def break_python(env, failure):
    python = Path(env["PATH"]) / "python3"
    python.unlink()
    if failure == "absent":
        assert shutil.which("python3", path=env["PATH"]) is None
        return
    condition = {
        "exit": "exit 1\n",
        "files": 'case "${1:-}" in -c|-V) ;; *) exit 1 ;; esac\n',
        "writer": 'case "${1:-}" in */write_backup_status.py) exit 1 ;; esac\n',
    }[failure]
    executable(python, condition + f'exec "{sys.executable}" "$@"\n')


@pytest.mark.parametrize("failure", ["exit", "absent", "files"])
def test_helper_failure_replaces_success_and_reaches_health(shell_env, sessions, failure):
    first = run("backup.sh", shell_env)
    assert first.returncode == 0, first.stderr
    directory = Path(shell_env["BACKUP_STATUS_HOST_DIR"])
    path = directory / "status.json"
    before = json.loads(path.read_text())["last_success_at"]
    original_dump = Path(first.stdout.strip()).read_bytes()
    break_python(shell_env, failure)

    with path.open() as old_inode:
        result = run("backup.sh", shell_env)
        assert json.load(old_inode)["last_attempt"]["result"] == "success"
    assert result.returncode != 0 and result.stdout == ""
    assert Path(first.stdout.strip()).read_bytes() == original_dump
    public = json.loads(path.read_text())
    assert public["last_attempt"]["result"] == "failure"
    assert public["last_success_at"] == before
    assert public["failure_reason"] == "host_helper_failed"
    assert "/" not in path.read_text()
    assert status(shell_env)["message"] not in path.read_text()
    assert status(shell_env)["result"] == "failure"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert not list(directory.glob(".status-*"))
    state = read_backup_status(path)
    assert state["status"] == "available"
    assert backup_reasons(state) == ["backup_failed"]
    report = report_for(sessions, path)
    assert "backup_failed" in report["reasons"]
    assert report["last_backup_success"] == before


def test_only_status_writer_failure_still_records_success(shell_env):
    # A flock-capable host uses the same file and holds an inherited descriptor.
    executable(
        Path(shell_env["PATH"]) / "flock",
        '[ "$*" = "-x 9" ] && [ -e /dev/fd/9 ] || exit 1\n'
        'printf "locked" > "$BACKUP_STATUS_HOST_DIR/lock-observed"\n',
    )
    break_python(shell_env, "writer")
    result = run("backup.sh", shell_env)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_file()
    path = Path(shell_env["BACKUP_STATUS_HOST_DIR"]) / "status.json"
    assert (path.parent / ".writer.lock").is_file()
    assert (path.parent / "lock-observed").read_text() == "locked"
    public = json.loads(path.read_text())
    assert public["last_attempt"]["result"] == "success"
    assert public["last_success_at"] == public["last_attempt"]["timestamp"]
    assert public["failure_reason"] == "host_helper_failed"
    assert read_backup_status(path)["status"] == "available"
    assert "/" not in path.read_text()
    assert status(shell_env)["message"] not in path.read_text()


@pytest.mark.parametrize("previous", [None, "not-a-time", "2026-01-01T00:00:00Z/private"])
def test_failure_without_valid_previous_success_uses_null(shell_env, previous):
    directory = Path(shell_env["BACKUP_STATUS_HOST_DIR"])
    directory.mkdir()
    path = directory / "status.json"
    if previous is not None:
        path.write_text(json.dumps({"last_success_at": previous}))
    break_python(shell_env, "exit")
    assert run("backup.sh", shell_env).returncode != 0
    public = json.loads(path.read_text())
    assert public["last_success_at"] is None
    assert read_backup_status(path)["status"] == "available"
    assert "/" not in path.read_text()


@pytest.mark.parametrize("tool", ["mktemp", "chmod", "mv", "flock"])
def test_shell_publication_error_is_nonfatal_and_sanitized(shell_env, tool):
    # Backup success is independent of status publication, even if fallback fails.
    break_python(shell_env, "writer")
    binary = Path(shell_env["PATH"]) / tool
    if binary.is_symlink():
        binary.unlink()
    executable(
        binary,
        'case "$*" in *metadata*) echo "/private/status secret" >&2; exit 1 ;; esac\n'
        + ("exit 1\n" if tool == "flock" else f'exec "{shutil.which(tool)}" "$@"\n'),
    )
    result = run("backup.sh", shell_env)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("backup: could not publish status metadata") == 1
    assert "/private/status secret" not in result.stderr
    assert status(shell_env)["result"] == "success"
    assert not list(Path(shell_env["BACKUP_STATUS_HOST_DIR"]).glob(".status-*"))
