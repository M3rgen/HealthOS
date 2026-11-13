"""Restore reports use the union of tables and fail closed on query errors."""

from pathlib import Path

import pytest
from support.backup_scripts import (
    DUMP,
    backup_env_file,
    comparisons,
    fake_sudo,
    run,
)
from support.backup_scripts import (
    count_docker as count_docker,
)
from support.backup_scripts import (
    pytestmark as pytestmark,
)
from support.backup_scripts import (
    sandbox as sandbox,
)


def test_restore_compares_union_of_tables_without_calling_changes_corruption(count_docker):
    env, dump = count_docker
    env.update(
        {
            "RESTORED_COUNTS": "same\t0\nchanged\t2\nold_table\t3\n",
            "LIVE_COUNTS": "changed\t9\nnew_table\t4\nsame\t0\n",
        }
    )
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout) == {
        "same": ["0", "0", "equal"],
        "changed": ["2", "9", "different"],
        "old_table": ["3", "-", "restored_only"],
        "new_table": ["-", "4", "live_only"],
    }
    assert "differences alone do not imply corruption" in result.stdout
    log = Path(env["FAKE_DOCKER_LOG"]).read_text()
    assert "dropdb -U health --if-exists health_restore" in log
    assert "dropdb -U health --if-exists health\n" not in log
    assert "pg_restore -U health --dbname=health_restore" in log
    assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP


@pytest.mark.parametrize("side", ["LIVE", "RESTORED"])
@pytest.mark.parametrize("failure", ["query", "malformed", "duplicate", "negative"])
def test_restore_query_failures_and_invalid_results_never_produce_a_comparison(
    count_docker,
    side,
    failure,
):
    env, dump = count_docker
    if failure == "query":
        env[f"FAIL_{side}_QUERY"] = "1"
    else:
        env[f"{side}_COUNTS"] = {
            "malformed": "same\toops\n",
            "duplicate": "same\t1\nsame\t1\n",
            "negative": "same\t-1\n",
        }[failure]
    result = run("restore.sh", env, str(dump))
    assert result.returncode != 0
    assert not comparisons(result.stdout)
    assert "comparison" not in result.stdout
    assert "/private/query" not in result.stderr


@pytest.mark.parametrize("target", ["health", "postgres", "template0", "template1", "bad;name"])
def test_comparison_changes_keep_scratch_database_guard(count_docker, target):
    env, dump = count_docker
    result = run("restore.sh", env, str(dump), target)
    assert result.returncode != 0
    assert not Path(env["FAKE_DOCKER_LOG"]).exists()


def test_successful_empty_live_query_is_distinct_from_a_query_error(count_docker):
    env, dump = count_docker
    env["LIVE_COUNTS"] = ""
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout) == {"same": ["2", "-", "restored_only"]}


@pytest.mark.parametrize("live", ["same\t2\n", ""])
def test_restore_without_tables_fails_explicitly(count_docker, live):
    env, dump = count_docker
    env.update({"RESTORED_COUNTS": "", "LIVE_COUNTS": live})
    result = run("restore.sh", env, str(dump))
    assert result.returncode != 0
    assert "restored database health_restore has no tables" in result.stderr
    assert "row-count query failed" not in result.stderr
    assert not comparisons(result.stdout) and "comparison" not in result.stdout


def test_large_integer_counts_are_compared_exactly(count_docker):
    env, dump = count_docker
    env.update(
        {"LIVE_COUNTS": "same\t9007199254740993\n", "RESTORED_COUNTS": "same\t9007199254740992\n"}
    )
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert comparisons(result.stdout)["same"][2] == "different"


def test_revision_query_error_is_not_a_successful_restore_report(count_docker):
    env, dump = count_docker
    result = run("restore.sh", {**env, "FAIL_REVISION_QUERY": "1"}, str(dump))
    assert result.returncode != 0
    assert "restored revision query failed" in result.stderr
    assert "Alembic revision:" not in result.stdout


@pytest.fixture
def identity_docker(count_docker):
    env, dump = count_docker
    encrypted = dump.with_suffix(".dump.age")
    encrypted.write_bytes(DUMP)
    age = Path(env["PATH"].split(":")[0]) / "age"
    age.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$3" > "$AGE_IDENTITY_LOG"\n'
        '[ -r "$3" ] || exit 1\ncat "$4"\n'
    )
    age.chmod(0o755)
    env["AGE_IDENTITY_LOG"] = str(dump.parent / "identity.log")
    return env, encrypted


def test_restore_key_file_ignores_inline_comment(identity_docker):
    env, dump = identity_docker
    key = Path(env.pop("BACKUP_KEY_FILE"))
    key.write_text("synthetic identity\n")
    backup_env_file(env, f"BACKUP_KEY_FILE={key}   # age identity\n")
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert Path(env["AGE_IDENTITY_LOG"]).read_text().strip() == str(key)
    assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP


@pytest.mark.parametrize("setting", ["BACKUP_AGE_IDENTITY", "BACKUP_KEY_FILE", "backup.env"])
def test_restore_relative_identity_uses_caller_directory(identity_docker, monkeypatch, setting):
    env, dump = identity_docker
    caller = dump.parent / "caller with spaces"
    (caller / "keys").mkdir(parents=True)
    key = caller / "keys" / "id.age"
    key.write_text("synthetic identity\n")
    if setting == "backup.env":
        env.pop("BACKUP_KEY_FILE")
        backup_env_file(env, "BACKUP_KEY_FILE=keys/id.age\n")
    else:
        env[setting] = "keys/id.age"
    monkeypatch.chdir(caller)
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert Path(env["AGE_IDENTITY_LOG"]).read_text().strip() == str(key)
    assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP


def test_restore_sudo_preserves_backup_env_file(sandbox, tmp_path):
    # An unavailable parent takes the same sudo branch as an unsearchable directory.
    dump = tmp_path / "unavailable" / "synthetic.dump.age"
    binary = Path(sandbox["PATH"].split(":")[0]) / "id"
    binary.write_text("#!/usr/bin/env bash\necho 1000\n")
    binary.chmod(0o755)
    calls = fake_sudo(sandbox, tmp_path)
    env = {**sandbox, "BACKUP_ENV_FILE": str(tmp_path / "custom backup.env")}
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert "rerunning with sudo" in result.stderr
    assert f"BACKUP_ENV_FILE={env['BACKUP_ENV_FILE']}" in calls.read_text()


def test_insufficient_restore_space_preserves_scratch_database(count_docker):
    env, dump = count_docker
    env.update({"LIVE_DATABASE_BYTES": "2147483648", "RESTORE_DF_OUTPUT": "fake 9 1 1048576 1% /"})
    result = run("restore.sh", env, str(dump))
    log = Path(env["FAKE_DOCKER_LOG"]).read_text()
    assert "dropdb" not in log
    assert result.returncode != 0
    assert "1.00 GiB" in result.stderr and "2.00 GiB" in result.stderr
    assert "docs/RUNBOOK.md#watch-the-disk" in result.stderr
    assert "createdb" not in log and "pg_restore" not in log


@pytest.mark.parametrize("free_kib", [2097152, 3145728])
def test_sufficient_restore_space_continues(count_docker, free_kib):
    env, dump = count_docker
    env.update(
        {"LIVE_DATABASE_BYTES": "2147483648", "RESTORE_DF_OUTPUT": f"fake 9 1 {free_kib} 1% /"}
    )
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    log = Path(env["FAKE_DOCKER_LOG"]).read_text()
    assert log.index("pg_database_size('health')") < log.index("dropdb")
    assert log.index("df -Pk /var/lib/postgresql/data") < log.index("dropdb")
    assert "createdb" in log and "pg_restore" in log
    assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP


@pytest.mark.parametrize(
    "setting,value",
    [
        ("RESTORE_DF_OUTPUT", ""),
        ("RESTORE_DF_OUTPUT", "fake 9 1 invalid 1% /"),
        ("LIVE_DATABASE_BYTES", ""),
        ("LIVE_DATABASE_BYTES", "invalid"),
        ("FAIL_SPACE_QUERY", "1"),
        ("FAIL_SIZE_QUERY", "1"),
    ],
)
def test_unavailable_restore_space_measurement_refuses_changes(count_docker, setting, value):
    env, dump = count_docker
    result = run("restore.sh", {**env, setting: value}, str(dump))
    assert result.returncode != 0
    log = Path(env["FAKE_DOCKER_LOG"]).read_text()
    assert all(command not in log for command in ("dropdb", "createdb", "pg_restore"))
    assert "refusing restore" in result.stderr and "unknown GiB" in result.stderr
    assert "docs/RUNBOOK.md#watch-the-disk" in result.stderr
