""".env backups of install.sh live outside the checkout (IP-8/5).

New copies go to ~/.local/state/healthos/env-backups/ (directory 700, files 600); older
.env.bak-* files next to .env are copied there once and never moved, changed or deleted.
Runs in the stub sandbox of test_installer_unix (HOME is a temporary directory).
"""

from __future__ import annotations

import stat
from pathlib import Path

from test_installer_unix import DRY, HOST_PLATFORM, REAL, Sandbox
from test_installer_unix import box as box
from test_installer_unix import pytestmark as pytestmark  # bash on a Unix host


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def store(box: Sandbox) -> Path:
    return box.home / ".local/state/healthos/env-backups"


def prefix(box: Sandbox) -> str:
    return str(box.repo.resolve()).lstrip("/").replace("/", "_")


def test_new_backups_go_to_the_private_state_directory(box):
    env = box.repo / ".env"
    env.write_text("POSTGRES_PASSWORD=keep\nAPP_UI_TOKEN=\n")
    env.chmod(0o644)
    result = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert result.returncode == 0, result.stdout + result.stderr
    (copy,) = store(box).iterdir()
    assert copy.name.startswith(f"{prefix(box)}.env.bak-")
    assert copy.read_text() == "POSTGRES_PASSWORD=keep\nAPP_UI_TOKEN=\n"
    assert mode(copy) == 0o600 and mode(store(box)) == 0o700
    assert not list(box.repo.glob(".env.bak-*"))  # nothing new next to .env
    out = " ".join(result.stdout.split())
    assert f".env backups of this checkout: 1 in {store(box)}" in out


def test_older_copies_are_copied_once_and_kept(box):
    env = box.repo / ".env"
    env.write_text("POSTGRES_PASSWORD=keep\nAPP_UI_TOKEN=\n")
    old = box.repo / ".env.bak-20260101T000000Z"
    old.write_text("POSTGRES_PASSWORD=older\n")
    old.chmod(0o644)
    first = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert first.returncode == 0, first.stdout + first.stderr
    assert old.read_text() == "POSTGRES_PASSWORD=older\n" and mode(old) == 0o644  # untouched
    copied = store(box) / f"{prefix(box)}.env.bak-20260101T000000Z"
    assert copied.read_text() == old.read_text() and mode(copied) == 0o600
    assert len(box.backups()) == 2  # the older copy and the new one
    # Re-run: nothing to fill, nothing copied twice.
    second = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(box.backups()) == 2 and "not modified" in second.stdout
    # A different file under a name already taken is added beside it, never over it.
    copied.write_text("POSTGRES_PASSWORD=edited-by-hand\n")
    third = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert third.returncode == 0, third.stdout + third.stderr
    assert copied.read_text() == "POSTGRES_PASSWORD=edited-by-hand\n"
    assert (copied.parent / f"{copied.name}-2").read_text() == old.read_text()
    assert old.exists()


def test_dry_run_only_says_where_backups_would_go(box):
    (box.repo / ".env").write_text("POSTGRES_PASSWORD=keep\nAPP_UI_TOKEN=\n")
    (box.repo / ".env.bak-20260101T000000Z").write_text("x=1\n")
    result = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not store(box).exists()
    out = " ".join(result.stdout.split())
    assert f"would copy .env to {store(box)}/{prefix(box)}.env.bak-<UTC stamp>" in out
    assert f"would copy {box.repo.resolve()}/.env.bak-20260101T000000Z to {store(box)}/" in out


def test_an_existing_open_backup_directory_is_reported_not_changed(box):
    folder = store(box)
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    (box.repo / ".env").write_text("APP_UI_TOKEN=\n")
    result = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert result.returncode == 0, result.stdout + result.stderr
    assert mode(folder) == 0o755
    assert "is open to other accounts" in result.stdout
    assert all(mode(path) == 0o600 for path in box.backups())
