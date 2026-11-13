"""How scripts/backup.sh takes the deploy user's .env from systemd's credential directory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from support.backup_scripts import fake_sudo, run, sandbox, write_key  # noqa: F401 - fixture


def _credential(tmp_path: Path, text: str) -> Path:
    directory = tmp_path / "credentials"
    directory.mkdir()
    (directory / "healthos.env").write_text(text, encoding="utf-8")
    return directory


def test_compose_gets_the_credential_copy_when_systemd_provides_one(sandbox, tmp_path):  # noqa: F811
    write_key(sandbox)
    credentials = _credential(tmp_path, "POSTGRES_PASSWORD=synthetic\n")
    result = run("backup.sh", {**sandbox, "CREDENTIALS_DIRECTORY": str(credentials)})
    assert result.returncode == 0, result.stderr
    logged = Path(sandbox["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    expected = f"compose --env-file {credentials}/healthos.env --profile backup run --rm -T backup"
    assert expected in logged


def test_settings_of_the_project_env_are_read_from_the_credential_copy(sandbox, tmp_path):  # noqa: F811
    write_key(sandbox)
    # Refuses to start only if BACKUP_MIN_FREE_MB from the credential copy is honoured.
    credentials = _credential(tmp_path, "BACKUP_MIN_FREE_MB=999999999\n")
    result = run("backup.sh", {**sandbox, "CREDENTIALS_DIRECTORY": str(credentials)})
    assert result.returncode == 1
    assert "BACKUP_MIN_FREE_MB=999999999" in result.stderr


def test_without_a_credential_compose_reads_the_project_env_as_before(sandbox):  # noqa: F811
    write_key(sandbox)
    result = run("backup.sh", sandbox)
    assert result.returncode == 0, result.stderr
    assert "--env-file" not in Path(sandbox["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can always write")
def test_root_owned_status_directory_reruns_under_sudo(sandbox, tmp_path):  # noqa: F811
    # After a timer run the status directory is root's; an operator's `make backup` must rerun
    # itself rather than end with an unpublished status.
    status_dir = Path(sandbox["BACKUP_STATUS_HOST_DIR"])
    status_dir.mkdir(mode=0o500)
    calls = fake_sudo(sandbox, tmp_path)
    try:
        result = run("backup.sh", sandbox)
    finally:
        status_dir.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert f"{status_dir} is writable only by root" in result.stderr
    assert calls.read_text(encoding="utf-8").strip().endswith("scripts/backup.sh")
