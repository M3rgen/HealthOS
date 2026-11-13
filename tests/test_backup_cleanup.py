"""Interrupted or empty backups leave no half-written file behind."""

from __future__ import annotations

import os
import shutil
import signal
from pathlib import Path

from support.backup_scripts import (  # noqa: F401 - fixtures
    PROCESS_GUARD,
    backup_process,
    fake_age,
    fixed_age,
    partials,
    run,
    sandbox,
    status,
    wait_for,
    write_key,
)


def test_a_cancelled_status_write_leaves_no_partial_status(fixed_age, tmp_path):  # noqa: F811
    binaries = Path(fixed_age["PATH"].split(os.pathsep)[0])
    real_mv = shutil.which("mv")
    # Only the first publication stalls: the failure status written on exit must go through.
    (binaries / "mv").write_text(
        "#!/usr/bin/env bash\n"
        '[ -e "$FAKE_STATUS_GATE.ready" ] && exec ' + f'"{real_mv}" "$@"\n'
        'touch "$FAKE_STATUS_GATE.ready"\n'
        "while :; do sleep 0.02; done\n"
    )
    (binaries / "mv").chmod(0o755)
    env = {**fixed_age, "FAKE_STATUS_GATE": str(tmp_path / "status")}
    backups = Path(env["BACKUP_HOST_DIR"])
    with backup_process(env) as process:
        wait_for(Path(env["FAKE_STATUS_GATE"] + ".ready"))
        assert list(backups.glob(".*.tmp.*"))  # the partial status exists right now
        os.killpg(process.pid, signal.SIGTERM)
        _, stderr = process.communicate(timeout=PROCESS_GUARD)
        assert process.returncode == 143, stderr
    assert not list(backups.glob(".*.tmp.*")) and not list(backups.glob("*.tmp*"))
    assert not partials(backups)


def test_an_empty_encrypted_dump_is_refused_not_published(fake_age):  # noqa: F811
    write_key(fake_age)
    result = run("backup.sh", {**fake_age, "FAKE_ENCRYPT_EMPTY": "1"})
    assert result.returncode == 1, result.stderr
    assert "encrypted dump is empty" in result.stderr
    backups = Path(fake_age["BACKUP_HOST_DIR"])
    assert not list(backups.glob("health-*.dump.age"))
    assert not partials(backups)
    report = status(fake_age)
    assert report["result"] == "failure" and report["file"] is None
