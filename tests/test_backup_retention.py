"""Retention is opt-in and preserves every unrelated synthetic backup."""

from pathlib import Path

import pytest
from support.backup_scripts import (
    backup_env_file,
    old_dumps,
    run,
    run_with_env_file,
    status,
)
from support.backup_scripts import (
    fake_age as fake_age,
)
from support.backup_scripts import (
    pytestmark as pytestmark,
)
from support.backup_scripts import (
    sandbox as sandbox,
)


@pytest.mark.parametrize("value", ["", "0", "never", "NEVER", "36500"])
def test_unset_zero_never_and_36500_retention_delete_nothing(fake_age, value):
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups, days=400)

    result = run("backup.sh", {**fake_age, "BACKUP_RETENTION_DAYS": value})

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    assert len(list(backups.glob("health-*.dump*"))) == len(dumps) + 1
    assert status(fake_age)["pruned_files"] == 0


def test_retention_from_env_file_36500_is_accepted(fake_age, tmp_path: Path):
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, _ = old_dumps(backups, days=400)

    result = run_with_env_file(tmp_path, fake_age, "BACKUP_RETENTION_DAYS=36500\n")

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in dumps)
    assert status(fake_age)["retention_days"] == "36500"


def test_a_positive_retention_in_env_file_prunes(fake_age, tmp_path: Path):
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)

    result = run_with_env_file(tmp_path, fake_age, "BACKUP_RETENTION_DAYS=7\n")

    assert result.returncode == 0, result.stderr
    assert not any(path.exists() for path in dumps)
    assert all(path.exists() for path in others)
    assert status(fake_age)["retention_days"] == "7"


@pytest.mark.parametrize("value", ["", "0", "never"])
def test_an_explicit_empty_or_never_environment_value_overrides_env_file(
    fake_age, tmp_path: Path, value
):
    # An empty BACKUP_RETENTION_DAYS in the environment used to fall back to .env's 7.
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)

    result = run_with_env_file(
        tmp_path, {**fake_age, "BACKUP_RETENTION_DAYS": value}, "BACKUP_RETENTION_DAYS=7\n"
    )

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    report = status(fake_age)
    assert report["retention_days"] == "never" and report["pruned_files"] == 0


def test_explicit_positive_retention_prunes_only_old_health_dumps(fake_age):
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    recent = backups / "health-20991231T000000Z.dump.age"
    recent.write_bytes(b"x")

    result = run("backup.sh", {**fake_age, "BACKUP_RETENTION_DAYS": "7"})

    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert output.exists() and recent.exists()
    assert not any(path.exists() for path in dumps)
    assert all(path.exists() for path in others)
    report = status(fake_age)
    assert report["retention_days"] == "7" and report["pruned_files"] == len(dumps)


@pytest.mark.parametrize("value", ["-5", "abc", "7d", " 7", "1e3", "99999999999999999999"])
def test_any_other_retention_value_keeps_every_backup(fake_age, value):
    # Neither deletes nor stops the backup: a typo must not cost a night's backup.
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)

    result = run("backup.sh", {**fake_age, "BACKUP_RETENTION_DAYS": value})

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    report = status(fake_age)
    assert report["result"] == "success"
    assert report["retention_days"] == "never" and report["pruned_files"] == 0
    if len(value) <= 9:
        assert "keeping every backup" in result.stderr


def test_retention_in_backup_env_is_honoured(fake_age):
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    backup_env_file(fake_age, "BACKUP_RETENTION_DAYS=7\n")
    result = run("backup.sh", {**fake_age})
    assert result.returncode == 0, result.stderr
    assert not any(path.exists() for path in dumps)
    assert all(path.exists() for path in others)
