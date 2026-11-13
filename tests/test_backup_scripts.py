"""Backup shell behavior against synthetic commands, without Docker."""

import json
import os
import shutil
import signal
import stat
from pathlib import Path

import pytest
from support.backup_scripts import (
    BASH,
    DUMP,
    GIB,
    PROCESS_GUARD,
    ROOT,
    backup_env_file,
    backup_process,
    fake_sudo,
    file_clock,
    needs_gpg,
    old_dumps,
    partials,
    run,
    status,
    wait_for,
    write_key,
)
from support.backup_scripts import (
    fake_age as fake_age,
)
from support.backup_scripts import (
    fixed_age as fixed_age,
)
from support.backup_scripts import (
    sandbox as sandbox,
)

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def test_old_host_python_fails_before_docker(fake_age):
    python = Path(fake_age["PATH"].split(os.pathsep)[0]) / "python3"
    python.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = -V ]; then\n'
        '  echo "Python 3.9.6" >&2; exit 0\nfi\nexit 1\n'
    )
    python.chmod(0o755)
    # The installer extension's backup.sh picks any Python 3.11+ it finds: name only the old one.
    fake_age["BACKUP_PYTHON"] = str(python)

    result = run("backup.sh", fake_age)

    assert result.returncode != 0
    assert (
        "backup: needs python3 >= 3.11 on the host (found: Python 3.9.6); see docs/DEPLOYMENT.md"
    ) in result.stderr
    assert "Traceback" not in result.stderr
    assert not Path(fake_age["FAKE_DOCKER_LOG"]).exists()


@needs_gpg
def test_backup_is_encrypted_timestamped_and_keeps_old_dumps_by_default(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run("backup.sh", sandbox)

    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert output.parent == backups
    assert output.name.startswith("health-") and output.name.endswith("Z.dump.gpg")
    ciphertext = output.read_bytes()
    assert DUMP not in ciphertext and not ciphertext.startswith(b"PGDMP")
    assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
    assert all(path.exists() for path in (*dumps, *others))
    assert "free space in" in result.stderr
    assert "--profile backup run --rm -T backup" in Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    report = status(sandbox)
    assert report["result"] == "success"
    assert report["file"] == str(output)
    assert report["size_bytes"] == len(ciphertext)
    assert report["free_bytes"] == 100 * GIB
    assert report["retention_days"] == "never" and report["pruned_files"] == 0

    restored = run("restore.sh", sandbox, str(output))

    assert restored.returncode == 0, restored.stderr
    assert Path(sandbox["FAKE_RESTORE_SINK"]).read_bytes() == DUMP
    assert "manual_measurements" in restored.stdout
    log = Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    assert "createdb -U health health_restore" in log
    assert "--dbname=health_restore" in log


def test_low_disk_refuses_to_start_writes_nothing_and_deletes_nothing(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)
    env = {**sandbox, "FAKE_DF_AVAIL_KB": str(512 * 1024), "BACKUP_RETENTION_DAYS": "7"}

    result = run("backup.sh", env)

    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert "No backup was written or deleted" in result.stderr
    assert result.stdout == ""
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()
    assert all(path.exists() for path in (*dumps, *others))
    assert len(list(backups.glob("health-*"))) == len(dumps)
    assert not partials(backups)
    report = status(sandbox)
    assert report["result"] == "failure" and "refusing to start" in report["message"]
    assert report["file"] is None
    assert report["free_bytes"] == GIB // 2 and report["required_free_bytes"] == GIB


def test_first_backup_needs_five_gib_by_default(sandbox):
    write_key(sandbox)
    result = run("backup.sh", {**sandbox, "FAKE_DF_AVAIL_KB": str(4 * 1024 * 1024)})
    assert result.returncode != 0
    assert "5 GiB before the first dump" in result.stderr
    assert status(sandbox)["required_free_bytes"] == 5 * GIB


def test_free_space_default_is_twice_the_newest_dump(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir()
    older = backups / "health-20250101T000000Z.dump.gpg"
    older.write_bytes(b"x")
    os.utime(older, (file_clock(backups) - 86_400,) * 2)
    newest = backups / "health-20250102T000000Z.dump.gpg"
    with newest.open("wb") as handle:  # sparse: 3 GiB apparent size, no real disk use
        handle.truncate(3 * GIB)
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "FAKE_DF_AVAIL_KB": str(5 * 1024 * 1024)})

    assert result.returncode != 0
    assert "twice the newest dump" in result.stderr
    assert status(sandbox)["required_free_bytes"] == 6 * GIB
    assert older.exists() and newest.exists() and not partials(backups)


@needs_gpg
def test_min_free_override_allows_a_small_disk(sandbox):
    write_key(sandbox)
    env = {**sandbox, "FAKE_DF_AVAIL_KB": "200000", "BACKUP_MIN_FREE_MB": "100"}
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert status(sandbox)["required_free_bytes"] == 100 * 1024 * 1024


@needs_gpg
def test_failed_dump_leaves_no_partial_and_records_failure(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    dumps, others = old_dumps(backups)
    write_key(sandbox)

    result = run("backup.sh", {**sandbox, "FAKE_DOCKER_FAIL": "1", "BACKUP_RETENTION_DAYS": "7"})

    assert result.returncode != 0
    assert "no backup was written" in result.stderr
    assert result.stdout == ""
    assert all(path.exists() for path in (*dumps, *others))
    assert len(list(backups.glob("health-*"))) == len(dumps)
    assert not partials(backups)
    report = status(sandbox)
    assert report["result"] == "failure" and report["file"] is None


def test_backup_refuses_group_readable_key_file(sandbox):
    write_key(sandbox, 0o640)
    result = run("backup.sh", sandbox)
    assert result.returncode != 0
    assert "chmod 600" in result.stderr
    assert not list(Path(sandbox["BACKUP_HOST_DIR"]).glob("health-*"))
    report = status(sandbox)
    assert report["result"] == "failure" and "chmod 600" in report["message"]


def test_backup_without_key_fails_and_points_away_from_env_file(sandbox):
    result = run("backup.sh", sandbox)
    assert result.returncode != 0
    assert "BACKUP_AGE_RECIPIENT" in result.stderr and "not belong in .env" in result.stderr
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()


def test_restore_refuses_live_database(sandbox, tmp_path: Path):
    dump = tmp_path / "health-20261020T033000Z.dump"
    dump.write_bytes(DUMP)
    result = run("restore.sh", sandbox, str(dump), "health")
    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()


def test_systemd_timer_runs_backup_script_daily():
    timer = (ROOT / "deploy" / "systemd" / "healthos-backup.timer").read_text(encoding="utf-8")
    service = (ROOT / "deploy" / "systemd" / "healthos-backup.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:30:00" in timer and "Persistent=true" in timer
    assert "scripts/backup.sh" in service and "EnvironmentFile=-/etc/healthos/" in service
    assert "OnFailure=healthos-backup-failure.service" in service
    failure = (ROOT / "deploy" / "systemd" / "healthos-backup-failure.service").read_text(
        encoding="utf-8"
    )
    assert "logger" in failure and "user.crit" in failure


@pytest.mark.parametrize("method", ["age", "gpg"])
def test_fixed_second_successes_preserve_existing_targets_and_restore(fixed_age, method):
    env = dict(fixed_age)
    binaries = Path(env["PATH"].split(os.pathsep)[0])
    if method == "gpg":
        shutil.copy(binaries / "age", binaries / "gpg")
        env.pop("BACKUP_AGE_RECIPIENT")
    write_key(env)
    backups = Path(env["BACKUP_HOST_DIR"])
    backups.mkdir()
    original = backups / f"health-20261025T033000Z.dump.{method}"
    original.write_bytes(b"ciphertext-header\nUEdETVAtc3ludGhldGljLWR1bXAK\n")
    snapshots = {original: original.read_bytes()}
    for _ in range(2):
        result = run("backup.sh", env)
        assert result.returncode == 0, result.stderr
        output = Path(result.stdout.strip())
        assert output not in snapshots
        assert output in list(backups.glob(f"health-*.dump.{method}"))
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        snapshots[output] = output.read_bytes()
        assert all(path.read_bytes() == data for path, data in snapshots.items())
    for path in snapshots:
        restored = run("restore.sh", env, str(path))
        assert restored.returncode == 0, restored.stderr
        assert Path(env["FAKE_RESTORE_SINK"]).read_bytes() == DUMP
    # The default free-space guard discovers new names; opt-in retention also does.
    for path in snapshots:
        os.utime(path, (file_clock(backups) - 20 * 86400,) * 2)
    result = run("backup.sh", {**env, "BACKUP_RETENTION_DAYS": "7"})
    assert result.returncode == 0, result.stderr
    assert status(env)["pruned_files"] == 3
    assert not any(path.exists() for path in snapshots)
    assert not partials(backups)


def test_concurrent_backups_and_legacy_status_have_private_temporary_files(fixed_age, tmp_path):
    write_key(fixed_age)
    binaries = Path(fixed_age["PATH"].split(os.pathsep)[0])
    real_mv = shutil.which("mv")
    (binaries / "mv").write_text(
        '#!/usr/bin/env bash\nprintf "%s" "$2" > "$FAKE_STATUS_GATE.source"\n'
        'touch "$FAKE_STATUS_GATE.ready"\n'
        'while [ ! -e "$FAKE_STATUS_GATE.release" ]; do sleep 0.02; done\n'
        f'exec "{real_mv}" "$@"\n'
    )
    (binaries / "mv").chmod(0o755)
    envs = [
        {
            **fixed_age,
            "FAKE_ENCRYPT_GATE": str(tmp_path / f"encrypt-{i}"),
            "FAKE_STATUS_GATE": str(tmp_path / f"status-{i}"),
            "FAKE_DUMP_TEXT": f"dump-{i}",
        }
        for i in range(2)
    ]
    backups = Path(fixed_age["BACKUP_HOST_DIR"])
    with backup_process(envs[0]) as first, backup_process(envs[1]) as second:
        for env in envs:
            wait_for(Path(env["FAKE_ENCRYPT_GATE"] + ".ready"))
        assert len(partials(backups)) == 2
        assert not list(backups.glob("health-*.dump.age"))
        for env in envs:
            Path(env["FAKE_ENCRYPT_GATE"] + ".release").touch()
        for env in envs:
            wait_for(Path(env["FAKE_STATUS_GATE"] + ".ready"))
        status_partials = [Path(env["FAKE_STATUS_GATE"] + ".source").read_text() for env in envs]
        assert len(set(status_partials)) == 2
        assert {json.loads(Path(p).read_text())["file"] for p in status_partials} == {
            str(p) for p in backups.glob("health-*.dump.age")
        }
        for env in envs:
            Path(env["FAKE_STATUS_GATE"] + ".release").touch()
        outputs = []
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=PROCESS_GUARD)
            assert process.returncode == 0, stderr
            assert "could not" not in stderr
            outputs.append(Path(stdout.strip()))
        assert len(set(outputs)) == 2
        for i, output in enumerate(outputs):
            assert run("restore.sh", fixed_age, str(output)).returncode == 0
            assert Path(fixed_age["FAKE_RESTORE_SINK"]).read_bytes() == f"dump-{i}\n".encode()
    assert not partials(backups) and not list(backups.glob("*.tmp*"))
    assert status(fixed_age)["file"] in {str(p) for p in outputs}


@pytest.mark.parametrize("failure", ["dump", "encryption", "TERM", "INT"])
def test_failed_or_cancelled_peer_cannot_remove_another_runs_files(fixed_age, tmp_path, failure):
    first = run("backup.sh", fixed_age)
    assert first.returncode == 0, first.stderr
    original = Path(first.stdout.strip())
    before = original.read_bytes()
    env = {**fixed_age, "FAKE_ENCRYPT_GATE": str(tmp_path / "peer")}
    if failure in ("dump", "encryption"):
        env["FAKE_DOCKER_FAIL" if failure == "dump" else "FAKE_ENCRYPT_FAIL"] = "1"
    other = {**fixed_age, "FAKE_ENCRYPT_GATE": str(tmp_path / "survivor")}
    backups = original.parent
    with backup_process(env) as peer, backup_process(other) as survivor:
        wait_for(Path(env["FAKE_ENCRYPT_GATE"] + ".ready"))
        wait_for(Path(other["FAKE_ENCRYPT_GATE"] + ".ready"))
        assert len(partials(backups)) == 2
        if failure in ("TERM", "INT"):
            os.killpg(peer.pid, getattr(signal, "SIG" + failure))
        else:
            Path(env["FAKE_ENCRYPT_GATE"] + ".release").touch()
        stdout, stderr = peer.communicate(timeout=PROCESS_GUARD)
        assert peer.returncode == (143 if failure == "TERM" else 130 if failure == "INT" else 1)
        assert not stdout, stderr
        assert len(partials(backups)) == 1
        assert original.read_bytes() == before
        assert status(fixed_age)["result"] == "failure"
        metadata = json.loads(
            (Path(fixed_age["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text()
        )
        assert metadata["last_success_at"] == "2026-10-25T03:30:00Z"
        Path(other["FAKE_ENCRYPT_GATE"] + ".release").touch()
        stdout, stderr = survivor.communicate(timeout=PROCESS_GUARD)
        assert survivor.returncode == 0, stderr
        assert Path(stdout.strip()).read_bytes() == before
    assert original.read_bytes() == before and not partials(backups)


def test_publication_error_preserves_output_and_success_evidence(fixed_age):
    first = run("backup.sh", fixed_age)
    assert first.returncode == 0, first.stderr
    original = Path(first.stdout.strip())
    before = original.read_bytes()
    binary = Path(fixed_age["PATH"].split(os.pathsep)[0]) / "python3"
    real_python = shutil.which("python3")
    binary.write_text(
        f'#!/usr/bin/env bash\n[ "${{3:-}}" != publish ] || exit 1\nexec "{real_python}" "$@"\n'
    )
    binary.chmod(0o755)
    result = run("backup.sh", fixed_age)
    assert result.returncode == 1 and result.stdout == ""
    assert "cannot publish" in result.stderr
    assert original.read_bytes() == before
    assert list(original.parent.glob("health-*.dump.age")) == [original]
    assert not partials(original.parent)
    assert status(fixed_age)["result"] == "failure"
    metadata = json.loads((Path(fixed_age["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_success_at"] == "2026-10-25T03:30:00Z"


@pytest.mark.parametrize("failure", ["FAKE_DOCKER_FAIL", "FAKE_ENCRYPT_FAIL"])
def test_shell_success_then_failure_preserves_success_in_separate_metadata(fake_age, failure):
    first = run("backup.sh", fake_age)
    assert first.returncode == 0, first.stderr
    metadata_path = Path(fake_age["BACKUP_STATUS_HOST_DIR"]) / "status.json"
    initial = json.loads(metadata_path.read_text())
    assert initial["last_attempt"]["result"] == "success"
    assert initial["last_success_at"] == status(fake_age)["timestamp"]
    assert Path(first.stdout.strip()).exists()
    failed = run("backup.sh", {**fake_age, failure: "1"})
    assert failed.returncode == 1
    latest = json.loads(metadata_path.read_text())
    assert latest["last_attempt"]["result"] == "failure"
    assert latest["last_success_at"] == initial["last_success_at"]
    assert status(fake_age)["result"] == "failure"  # legacy consumers still work
    assert status(fake_age)["file"] is None
    assert not partials(Path(fake_age["BACKUP_HOST_DIR"]))
    assert Path(first.stdout.strip()).exists()
    for value in (fake_age["BACKUP_HOST_DIR"], fake_age["BACKUP_KEY_FILE"], "message", "file"):
        assert value not in metadata_path.read_text()


@pytest.mark.parametrize("failure", ["FAKE_DOCKER_FAIL", "FAKE_ENCRYPT_FAIL"])
def test_first_incomplete_encrypted_output_cannot_record_success(fake_age, failure):
    result = run("backup.sh", {**fake_age, failure: "1"})
    assert result.returncode == 1
    report = json.loads((Path(fake_age["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert report["last_success_at"] is None
    assert report["last_attempt"]["result"] == "failure"
    assert not list(Path(fake_age["BACKUP_HOST_DIR"]).glob("health-*"))


def test_shell_missing_key_records_failure_without_any_success(sandbox):
    result = run("backup.sh", sandbox)
    assert result.returncode == 1
    metadata = json.loads((Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_success_at"] is None
    assert metadata["last_attempt"]["result"] == "failure"


def test_shell_legacy_success_is_migrated_before_failure_overwrites_legacy(sandbox):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir()
    before = "2026-01-01T00:00:00Z"
    (backups / "last-backup-status.json").write_text(
        json.dumps(
            {
                "result": "success",
                "timestamp": before,
                "file": "/private/archive",
            }
        )
    )
    result = run("backup.sh", sandbox)  # no key -> failure
    assert result.returncode == 1
    metadata = json.loads((Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_success_at"] == before
    assert metadata["last_attempt"]["result"] == "failure"
    assert status(sandbox)["result"] == "failure"
    assert "/private" not in json.dumps(metadata)


def test_shell_metadata_publication_failure_is_sanitized_and_keeps_legacy(sandbox):
    directory = Path(sandbox["BACKUP_STATUS_HOST_DIR"])
    directory.write_text("not a directory")
    result = run("backup.sh", sandbox)
    assert result.returncode == 1
    assert "could not publish status metadata" in result.stderr
    assert str(directory) not in result.stderr
    assert "Traceback" not in result.stderr
    assert status(sandbox)["result"] == "failure"


def test_recipient_comes_from_backup_env_like_the_systemd_unit_gives_it(fake_age):
    env = {**fake_age}
    del env["BACKUP_AGE_RECIPIENT"]
    backup_env_file(env, "BACKUP_AGE_RECIPIENT=age1-from-backup-env\n")
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(".dump.age")


def test_the_environment_beats_backup_env(fake_age):
    backup_env_file(fake_age, "BACKUP_HOST_DIR=/nonexistent/from-backup-env\n")
    result = run("backup.sh", {**fake_age})
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).parent == Path(fake_age["BACKUP_HOST_DIR"])


@needs_gpg
def test_key_file_from_backup_env_is_used(sandbox, tmp_path):
    key = tmp_path / "other.key"
    key.write_text("synthetic-test-passphrase-not-a-secret\n", encoding="utf-8")
    key.chmod(0o600)
    env = {**sandbox}
    del env["BACKUP_KEY_FILE"]
    backup_env_file(env, f"BACKUP_KEY_FILE={key}\n")
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith(".dump.gpg")


@pytest.mark.skipif(os.geteuid() == 0, reason="a root-only file is readable by root")
def test_root_only_backup_env_reruns_under_sudo(sandbox, tmp_path):
    path = backup_env_file(sandbox, "BACKUP_AGE_RECIPIENT=age1-from-backup-env\n")
    path.chmod(0o000)
    calls = fake_sudo(sandbox, tmp_path)
    try:
        result = run("backup.sh", sandbox)
    finally:
        path.chmod(0o600)
    assert result.returncode == 0, result.stderr
    assert "readable only by root" in result.stderr and "sudo" in result.stderr
    assert calls.read_text(encoding="utf-8").strip().endswith("scripts/backup.sh")


@pytest.mark.skipif(os.geteuid() == 0, reason="a root-only directory is readable by root")
def test_backup_env_inside_a_root_only_directory_reruns_under_sudo(sandbox, tmp_path):
    # DEPLOYMENT step 8: /etc/healthos is mode 700, so the file inside cannot even be stat'ed.
    private = tmp_path / "healthos"
    private.mkdir()
    env = {**sandbox, "BACKUP_ENV_FILE": str(private / "backup.env")}
    backup_env_file(env, "BACKUP_AGE_RECIPIENT=age1-from-backup-env\n")
    calls = fake_sudo(sandbox, tmp_path)
    private.chmod(0o000)
    try:
        result = run("backup.sh", env)
    finally:
        private.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert "accessible only by root" in result.stderr
    logged = calls.read_text(encoding="utf-8")
    assert f"BACKUP_ENV_FILE={private / 'backup.env'}" in logged
    assert logged.strip().endswith("scripts/backup.sh")


@pytest.mark.skipif(os.geteuid() == 0, reason="a root-only directory is readable by root")
def test_restore_reruns_under_sudo_when_the_backup_directory_is_root_only(sandbox, tmp_path):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir()
    dump = backups / "health-20250101T000000Z.dump.age"
    dump.write_bytes(b"ciphertext")
    calls = fake_sudo(sandbox, tmp_path)
    backups.chmod(0o000)
    try:
        result = run("restore.sh", {**sandbox, "BACKUP_AGE_IDENTITY": "/k/id"}, str(dump))
    finally:
        backups.chmod(0o700)
    assert result.returncode == 0, result.stderr
    logged = calls.read_text(encoding="utf-8")
    assert "BACKUP_AGE_IDENTITY=/k/id" in logged and logged.strip().endswith(str(dump))


@pytest.mark.skipif(os.geteuid() == 0, reason="a root-only directory is writable by root")
def test_root_only_backup_directory_reruns_under_sudo(sandbox, tmp_path):
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    backups.mkdir(mode=0o500)
    calls = fake_sudo(sandbox, tmp_path)
    try:
        result = run("backup.sh", sandbox)
    finally:
        backups.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert "writable only by root" in result.stderr
    logged = calls.read_text(encoding="utf-8").strip()
    assert logged.endswith("scripts/backup.sh")
    # sudo resets the environment; the settings given to this run must survive it.
    assert f"BACKUP_HOST_DIR={backups}" in logged


@pytest.mark.skipif(os.geteuid() == 0, reason="a root-only file is readable by root")
def test_restore_reruns_under_sudo_when_the_backup_is_root_only(sandbox, tmp_path):
    backup = Path(sandbox["BACKUP_HOST_DIR"])
    backup.mkdir()
    dump = backup / "health-20250101T000000Z.dump.age"
    dump.write_bytes(b"ciphertext")
    dump.chmod(0o000)
    calls = fake_sudo(sandbox, tmp_path)
    try:
        result = run("restore.sh", {**sandbox, "BACKUP_AGE_IDENTITY": "/k/id"}, str(dump))
    finally:
        dump.chmod(0o600)
    assert result.returncode == 0, result.stderr
    assert "scripts/restore.sh" in calls.read_text(encoding="utf-8")
    assert "BACKUP_AGE_IDENTITY=/k/id" in calls.read_text(encoding="utf-8")


def test_backup_without_any_key_names_backup_env(sandbox):
    result = run("backup.sh", sandbox)
    assert result.returncode == 1
    assert "/etc/healthos/backup.env" in result.stderr
