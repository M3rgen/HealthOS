"""Empty dump detection, Compose-style comments and restore compatibility gates."""

import io
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from test_backup_scripts import (  # noqa: F401 - fixtures
    BASH,
    DUMP,
    ROOT,
    fake_age,
    old_dumps,
    partials,
    run,
    sandbox,
    status,
)

from app.services.operations.backup_files import forward_nonempty


def test_plaintext_counter_forwards_binary_data_in_bounded_chunks():
    class BoundedReader(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    payload = bytes(range(256)) * 12000
    output = io.BytesIO()
    assert forward_nonempty(BoundedReader(payload), output) == len(payload)
    assert output.getvalue() == payload
    with pytest.raises(ValueError, match="pg_dump stream is empty"):
        forward_nonempty(io.BytesIO(), io.BytesIO())


@pytest.mark.parametrize("previous_success", [False, True])
def test_empty_dump_keeps_files_and_success_metadata(fake_age, previous_success):  # noqa: F811
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    snapshots = {path: path.read_bytes() for path in (*dumps, *others)}
    metadata_path = Path(fake_age["BACKUP_STATUS_HOST_DIR"]) / "status.json"
    if previous_success:
        first = run("backup.sh", fake_age)
        assert first.returncode == 0, first.stderr
        output = Path(first.stdout.strip())
        snapshots[output] = output.read_bytes()
        success_at = json.loads(metadata_path.read_text())["last_success_at"]
    else:
        success_at = None
    # The fake encryptor emits a nonempty header even for empty stdin.
    docker = Path(fake_age["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    result = run("backup.sh", {**fake_age, "BACKUP_RETENTION_DAYS": "7"})
    assert result.returncode == 1 and result.stdout == ""
    assert "pg_dump stream is empty" in result.stderr
    assert all(path.read_bytes() == data for path, data in snapshots.items())
    assert set(backups.glob("health-*")) == {p for p in snapshots if p not in others}
    assert not partials(backups)
    assert status(fake_age)["result"] == "failure"
    assert status(fake_age)["pruned_files"] == 0
    assert json.loads(metadata_path.read_text())["last_success_at"] == success_at


@pytest.mark.parametrize(
    "value,expected",
    [
        ("36500 # keep old dumps", "36500"),
        ('"36500" # comment', "36500"),
        ("'36500' # comment", "36500"),
        ("36500\t# comment\r", "36500"),
        ("36500   ", "36500"),
        (" # empty value", "never"),
        ("36500#literal", "never"),
        ('"36500 # literal" # comment', "never"),
        ("'36500 # literal' # comment", "never"),
    ],
)
def test_retention_comments_match_compose(fake_age, tmp_path, value, expected):  # noqa: F811
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "healthos.env").write_text(f"BACKUP_RETENTION_DAYS={value}\n")
    result = run("backup.sh", {**fake_age, "CREDENTIALS_DIRECTORY": str(credentials)})
    assert result.returncode == 0, result.stderr
    assert status(fake_age)["retention_days"] == expected


@pytest.mark.parametrize("quoted", ["", "'", '"'])
def test_project_env_paths_preserve_hashes_and_spaces(fake_age, tmp_path, quoted):  # noqa: F811
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    destination = tmp_path / "backup#archive with spaces"
    (credentials / "healthos.env").write_text(
        "BACKUP_MIN_FREE_MB=999999999\n"
        "export BACKUP_MIN_FREE_MB=0 # last value wins\n"
        f"BACKUP_HOST_DIR={quoted}{destination}{quoted} # destination\n"
    )
    env = {**fake_age, "CREDENTIALS_DIRECTORY": str(credentials)}
    del env["BACKUP_HOST_DIR"]
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).parent == destination
    assert status({**env, "BACKUP_HOST_DIR": str(destination)})["required_free_bytes"] == 0


@pytest.fixture
def revision_docker(sandbox, tmp_path):  # noqa: F811
    docker = Path(sandbox["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text("""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"df -Pk /var/lib/postgresql/data"*)
    [ -z "${FAIL_SPACE_QUERY:-}" ] || exit 1
    printf '%s\\n' "${RESTORE_DF_OUTPUT-fake 999999999 1 104857600 1% /}" ;;
  *pg_database_size*)
    [ -z "${FAIL_SIZE_QUERY:-}" ] || exit 1
    printf '%s\\n' "${LIVE_DATABASE_BYTES-1073741824}" ;;
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*)
    case " $* " in
      *" -d health "*)
        [ -z "${FAIL_LIVE_REVISION:-}" ] || exit 1
        printf '%s' "$LIVE_REVISION" ;;
      *)
        [ -z "${FAIL_DUMP_REVISION:-}" ] || exit 1
        printf '%s' "$DUMP_REVISION" ;;
    esac ;;
  *psql*) printf 'raw_health_data\\t1\\n' ;;
esac
""")
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(DUMP)
    return {**sandbox, "LIVE_REVISION": "20261108_0026", "DUMP_REVISION": "20261108_0026"}, dump


@pytest.mark.parametrize("side", ["LIVE", "DUMP"])
@pytest.mark.parametrize("value", ["20261017_0007", "20270101_9999", ""])
def test_different_or_missing_schema_revision_refuses_success(revision_docker, side, value):
    env, dump = revision_docker
    result = run("restore.sh", {**env, f"{side}_REVISION": value}, str(dump))
    assert result.returncode == 1
    assert (
        "schema version mismatch" in result.stderr if value else "unknown schema" in result.stderr
    )
    if value:
        assert value in result.stderr and "20261108_0026" in result.stderr
        assert "Scratch database 'health_restore' was kept" in result.stderr
    assert "comparison" not in result.stdout and "Alembic revision:" not in result.stdout
    commands = Path(env["FAKE_DOCKER_LOG"]).read_text().splitlines()
    assert sum("dropdb" in command for command in commands) == 1
    assert all(
        "dropdb" not in command or command.endswith(" health_restore") for command in commands
    )
    assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP


@pytest.mark.parametrize("side", ["LIVE", "DUMP"])
def test_failed_schema_query_is_not_compatible(revision_docker, side):
    env, dump = revision_docker
    result = run("restore.sh", {**env, f"FAIL_{side}_REVISION": "1"}, str(dump))
    assert result.returncode == 1 and "revision query failed" in result.stderr
    assert "comparison" not in result.stdout


def test_matching_schema_uses_atomic_restore_and_reports_revision(revision_docker):
    env, dump = revision_docker
    result = run("restore.sh", env, str(dump))
    assert result.returncode == 0, result.stderr
    assert "Alembic revision: 20261108_0026" in result.stdout
    log = Path(env["FAKE_DOCKER_LOG"]).read_text()
    assert "--single-transaction --exit-on-error" in log


@pytest.mark.parametrize("failure", ["decrypt", "restore", "none"])
def test_documented_live_restore_stops_before_restart_on_pipeline_failure(sandbox, failure):  # noqa: F811
    document = (ROOT / "docs/reference/data-safety.md").read_text()
    recipe = document.split("bash <<'RESTORE'\n", 1)[1].split("\nRESTORE", 1)[0]
    drill = (ROOT / "scripts/verify_compose.py").read_text()
    drill = drill.split("def restore_over_live_database", 1)[1]
    drill = drill.split('"pg_restore",', 1)[1].split("cwd=ROOT", 1)[0]
    restore = recipe.split("pg_restore", 1)[1].split("\ndocker", 1)[0]
    assert re.findall(r'"(--[\w-]+)"', drill) == re.findall(r"--[\w-]+", restore)
    binaries = Path(sandbox["PATH"].split(os.pathsep)[0])
    age = binaries / "age"
    age.write_text('#!/usr/bin/env bash\nprintf "synthetic dump"\nexit "$DECRYPT_RC"\n')
    age.chmod(0o755)
    (binaries / "docker").write_text("""#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in *pg_restore*) cat >/dev/null; exit "$RESTORE_RC" ;; esac
case " $* " in
  *" -T "*) ;;
  *" compose run "*|*" compose exec "*) cat >/dev/null ;;
esac
""")
    result = subprocess.run(
        [BASH],
        input=recipe,
        env={
            **sandbox,
            "DECRYPT_RC": "1" if failure == "decrypt" else "0",
            "RESTORE_RC": "1" if failure == "restore" else "0",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == (0 if failure == "none" else 1), result.stderr
    log = Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    assert "--single-transaction --exit-on-error" in log
    assert ("provision-app-role" in log) == (failure == "none")
    assert ("start app" in log) == (failure == "none")
    assert ("healthos verify" in log) == (failure == "none")
    if failure == "none":
        steps = ["provision-app-role", "start app", "healthos verify"]
        assert sorted(steps, key=log.index) == steps
