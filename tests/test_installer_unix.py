"""install.sh in a temporary checkout copy: dry runs, .env safety, idempotency, bad arguments.

Docker, git, systemctl, launchctl and the other host tools are stubs on PATH that only log
their arguments, so nothing here needs Docker, root or a network. On macOS /bin/bash is 3.2,
which is the oldest bash the installer must support.
"""

from __future__ import annotations

import base64
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
IS_MAC = sys.platform == "darwin"
HOST_PLATFORM = "macos" if IS_MAC else "linux"

pytestmark = pytest.mark.skipif(
    BASH is None or sys.platform.startswith("win"), reason="bash on a Unix host is required"
)

STUB = """#!/bin/sh
echo "{name} $*" >> "$STUB_LOG"
case "{name} $*" in
  "docker compose version --short") echo 2.30.0 ;;
  "docker info --format"*) echo 27.3.1 ;;
  "docker context show") echo default ;;
  "git --version") echo "git version 2.45.0" ;;
  "git clone"*|"git -C"*) exit 1 ;;
  "age-keygen -o"*) printf 'AGE-SECRET-KEY-1SYNTHETIC\\n' > "$2" ;;
  "age-keygen -y"*) echo age1synthetic ;;
  "systemctl is-enabled"*) echo enabled ;;
  "launchctl print"*) exit 1 ;;
esac
exit 0
"""
STUBS = ("docker", "git", "systemctl", "launchctl", "loginctl", "uv", "brew", "age", "age-keygen")
# `id` answers from FAKE_UID / FAKE_GID / FAKE_USER when set; `getent` prints GETENT_GROUP /
# GETENT_PASSWD. The sandbox default is a private primary group (gid 4242, no other members),
# so a Linux host's real primary group does not decide whether a source is switched on.
ID_STUB = """#!/bin/sh
case "$1" in
  -u) [ -z "${FAKE_UID:-}" ] || { echo "$FAKE_UID"; exit 0; } ;;
  -g) [ -z "${FAKE_GID:-}" ] || { echo "$FAKE_GID"; exit 0; } ;;
  -un) [ -z "${FAKE_USER:-}" ] || { echo "$FAKE_USER"; exit 0; } ;;
esac
exec /usr/bin/id "$@"
"""
GETENT_STUB = """#!/bin/sh
case "$1" in
  group) [ -z "${GETENT_GROUP:-}" ] || printf '%s\\n' "$GETENT_GROUP" ;;
  passwd) [ -z "${GETENT_PASSWD:-}" ] || printf '%s\\n' "$GETENT_PASSWD" ;;
esac
exit 0
"""
LINUX_UNAME = '#!/bin/sh\ncase "$1" in -m) echo x86_64 ;; *) echo Linux ;; esac\n'
MANAGED = {
    "POSTGRES_PASSWORD",
    "APP_UI_TOKEN",
    "COMPOSE_FILE",
    "COMPOSE_PATH_SEPARATOR",
    "GOOGLE_TOKEN_ENCRYPTION_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION",
    "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI",
    "GARMIN_ENABLED",
    "GARMIN_API_KEY_HOST_FILE",
    "GARMIN_API_KEY_HOST_GID",
    "APPLE_RECEIVER_ENABLED",
    "APPLE_RECEIVER_TOKEN_HOST_FILE",
    "APPLE_RECEIVER_TOKEN_HOST_GID",
    "APPLE_RECEIVER_ENDPOINT_URI",
    "CODEX_BRIDGE_TOKEN",
}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export").strip()
        values[key] = value.strip().strip("\"'")
    return values


class Sandbox:
    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        for name in ("install.sh", ".env.example"):
            shutil.copy2(ROOT / name, self.repo / name)
        for path in ROOT.glob("docker-compose*.yml"):
            shutil.copy2(path, self.repo / path.name)
        shutil.copytree(ROOT / "deploy", self.repo / "deploy")
        (self.repo / "scripts").mkdir()
        for name in ("backup.sh", "restore.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.repo / "scripts" / name)
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "stub.log"
        self.log.touch()
        for name in STUBS:
            self.stub(name, STUB.replace("{name}", name))
        self.stub("id", ID_STUB)
        self.stub("getent", GETENT_STUB)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "STUB_LOG": str(self.log),
            "LC_ALL": "C",
            "TMPDIR": str(tmp_path),
            "FAKE_GID": "4242",
        }

    def stub(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(self.repo / "install.sh"), *args],
            cwd=cwd or self.repo,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def files(self) -> dict[str, bytes]:
        """Every file in the checkout's top level with its bytes (to prove nothing changed)."""
        return {p.name: p.read_bytes() for p in self.repo.iterdir() if not p.is_dir()}

    def source(self, snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run SNIPPET with install.sh's functions loaded (main is not called)."""
        text = (ROOT / "install.sh").read_text(encoding="utf-8").rstrip()
        assert text.endswith('main "$@"')
        lib = self.repo.parent / "install-lib.sh"
        lib.write_text(text.removesuffix('main "$@"'), encoding="utf-8")
        return subprocess.run(
            [BASH, "-c", f'. "{lib}"; LANG_SEL=en; {snippet}'],
            cwd=self.repo,
            env={**self.env, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def backups(self) -> list[Path]:
        return sorted(self.repo.glob(".env.bak-*"))


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_secrets(values: dict[str, str]) -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", values["POSTGRES_PASSWORD"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", values["APP_UI_TOKEN"])


def assert_fernet(key: str) -> None:
    assert len(key) == 44 and key.endswith("=")
    assert len(base64.urlsafe_b64decode(key)) == 32


DRY = ("--dry-run", "--non-interactive")


@pytest.mark.parametrize(
    ("platform", "mode", "sources", "extra", "compose"),
    [
        ("linux", "trial", "none", (), "docker-compose.yml:docker-compose.dev.yml"),
        (
            "linux",
            "full",
            "google",
            ("--google-client-id", "synthetic-id.apps.example", "--google-client-secret", "s3cr"),
            "docker-compose.yml",
        ),
        (
            "raspberrypi",
            "trial",
            "garmin",
            (),
            "docker-compose.yml:docker-compose.dev.yml:docker-compose.garmin.yml",
        ),
        (
            "macos",
            "trial",
            "apple",
            ("--apple-receiver", "yes"),
            "docker-compose.yml:docker-compose.dev.yml:docker-compose.apple.yml",
        ),
        ("vps", "full", "google,garmin,apple", (), "docker-compose.yml:docker-compose.garmin.yml"),
        ("nas", "trial", "none", ("--backups", "yes"), "docker-compose.yml:docker-compose.dev.yml"),
    ],
)
def test_dry_run_writes_only_the_preview(box, platform, mode, sources, extra, compose):
    key = box.home / "garmin.key"
    key.write_text("synthetic-garmin-key\n")
    args = [*DRY, "--platform", platform, "--mode", mode, "--sources", sources, *extra]
    if "garmin" in sources:
        args += ["--garmin-key-file", str(key)]
    result = box.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    out = box.repo / ".env.dry-run"
    assert out.stat().st_mode & 0o777 == 0o600
    assert not (box.repo / ".env").exists() and not box.backups()
    values = parse_env(out)
    example = parse_env(box.repo / ".env.example")
    assert_secrets(values)
    assert values["COMPOSE_FILE"] == compose
    assert values["COMPOSE_PATH_SEPARATOR"] == ":"
    assert values["APP_ENV"] == "development"  # Compose sets the runtime mode, not .env
    # Everything the installer does not manage is exactly the template.
    for name, value in example.items():
        if name not in MANAGED:
            assert values[name] == value, name
    if "google" in sources or mode == "full":
        assert_fernet(values["GOOGLE_TOKEN_ENCRYPTION_KEY"])
    else:
        assert values["GOOGLE_TOKEN_ENCRYPTION_KEY"] == ""
    if mode == "full":
        assert re.fullmatch(
            r"Bearer [A-Za-z0-9_-]{43}", values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"]
        )
        assert "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI" in result.stdout  # explained, not failed late
    if "--google-client-id" in extra:
        assert values["GOOGLE_CLIENT_ID"] == "synthetic-id.apps.example"
        assert values["GOOGLE_CLIENT_SECRET"] == "s3cr"
    if "garmin" in sources:
        assert values["GARMIN_ENABLED"] == "true"
        assert values["GARMIN_API_KEY_HOST_FILE"] == str(key.resolve())
        assert values["GARMIN_API_KEY_HOST_GID"].isdigit()
    else:
        assert values["GARMIN_ENABLED"] == "false"
    if "--apple-receiver" in extra:
        assert values["APPLE_RECEIVER_ENABLED"] == "true"
        assert values["APPLE_RECEIVER_TOKEN_HOST_FILE"].startswith(str(box.home))
        assert not Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"]).exists()
    # Nothing was cloned, built, started or installed.
    calls = box.calls()
    assert not [c for c in calls if c.startswith(("git clone", "docker compose up"))]
    assert not [c for c in calls if c.startswith(("systemctl", "launchctl", "loginctl", "brew"))]
    assert not [c for c in calls if c.startswith("age-keygen -o")]
    # (macOS's own python3 may cache under ~/Library/Caches; that is not the installer.)
    for created in (".config", "Library/LaunchAgents", "Library/Application Support"):
        assert not (box.home / created).exists(), created
    assert "[dry-run]" in result.stdout and "UI token" in result.stdout
    if platform == "nas":
        assert "Synology" in result.stdout and "systemctl" not in result.stdout
    if platform == "macos":
        assert "org.healtos" not in result.stdout  # backups default off in trial


def test_dry_run_shows_host_jobs_per_platform(box):
    mac = box.run(*DRY, "--platform", "macos", "--mode", "full", "--sources", "none")
    assert mac.returncode == 0, mac.stderr
    assert "Library/LaunchAgents/org.healtos.backup.plist" in mac.stdout
    assert "launchctl bootstrap" in mac.stdout
    assert "Application Support/HealtOS/backup.key" in mac.stdout
    linux = box.run(*DRY, "--platform", "linux", "--backups", "yes", "--umay", "yes")
    assert linux.returncode == 0, linux.stderr
    if IS_MAC or Path("/run/systemd/system").is_dir():  # a preview for another host assumes systemd
        assert "healthos-backup.timer" in linux.stdout
        assert "healthos-bridge.service" in linux.stdout
    else:
        assert "30 3 * * *" in linux.stdout  # cron line without systemd
    assert "OpenAI" in linux.stdout  # Umay privacy note
    assert not [c for c in box.calls() if c.startswith(("systemctl", "launchctl", "uv sync"))]


def test_existing_secrets_are_kept_and_selections_follow_the_choices(box):
    original = (
        "# my settings\n"
        "POSTGRES_PASSWORD=keep-this-database-password\n"
        "APP_UI_TOKEN=\n"
        "APP_PORT=8123\n"
        "COMPOSE_FILE=docker-compose.yml:my-override.yml\n"
        "GARMIN_ENABLED=false\n"
        "APPLE_RECEIVER_ENABLED=true\n"
        'GOOGLE_CLIENT_ID="quoted-existing-id"\n'
        "LOG_LEVEL=DEBUG\n"
    )
    env = box.repo / ".env"
    env.write_text(original)
    env.chmod(0o600)
    key = box.home / "garmin.key"
    key.write_text("k\n")
    result = box.run(
        *DRY,
        "--platform",
        HOST_PLATFORM,
        "--sources",
        "google,garmin",
        "--garmin-key-file",
        str(key),
        "--google-client-id",
        "other-id",
    )
    assert result.returncode == 0, result.stderr
    assert env.read_text() == original and not box.backups()
    preview = (box.repo / ".env.dry-run").read_text()
    values = parse_env(box.repo / ".env.dry-run")
    followed = ("APP_UI_TOKEN=", "COMPOSE_FILE=", "GARMIN_ENABLED=")
    for line in original.splitlines():
        if not line.startswith(followed):
            assert line in preview.splitlines(), line
    # Secrets and everything else are kept; an empty secret is generated.
    assert values["POSTGRES_PASSWORD"] == "keep-this-database-password"
    assert values["GOOGLE_CLIENT_ID"] == "quoted-existing-id"
    assert values["LOG_LEVEL"] == "DEBUG" and values["APP_PORT"] == "8123"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", values["APP_UI_TOKEN"])
    assert_fernet(values["GOOGLE_TOKEN_ENCRYPTION_KEY"])
    assert "http://localhost:8123" in result.stdout
    assert "GOOGLE_CLIENT_ID already has a value" in result.stdout
    # Selections follow: the existing COMPOSE_FILE has no development override, so the mode
    # stays full; the user's own file is kept after the installer's files; a chosen source is
    # switched on, never off.
    expected = "docker-compose.yml:docker-compose.garmin.yml:my-override.yml"
    assert values["COMPOSE_FILE"] == expected
    assert "COMPOSE_FILE changed from 'docker-compose.yml:my-override.yml'" in result.stdout
    assert values["GARMIN_ENABLED"] == "true"
    assert values["GARMIN_API_KEY_HOST_FILE"] == str(key.resolve())
    # Left on without a token path: kept on, reported, nothing to mount.
    assert values["APPLE_RECEIVER_ENABLED"] == "true"
    assert "APPLE_RECEIVER_ENABLED is on in .env but APPLE_RECEIVER_TOKEN_HOST_FILE is empty" in (
        result.stdout
    )


REAL = ("--non-interactive", "--no-start", "--backups", "no", "--autostart", "no", "--umay", "no")


def test_real_run_backs_up_then_fills_only_empty_keys_and_is_idempotent(box):
    env = box.repo / ".env"
    original = "POSTGRES_PASSWORD=existing-secret\nAPP_UI_TOKEN=\nLOG_LEVEL=WARNING\n"
    env.write_text(original)
    env.chmod(0o600)
    first = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert first.returncode == 0, first.stdout + first.stderr
    backups = box.backups()
    assert len(backups) == 1 and backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    text = env.read_text()
    assert text.startswith("POSTGRES_PASSWORD=existing-secret\nAPP_UI_TOKEN=")
    assert "LOG_LEVEL=WARNING\n" in text
    values = parse_env(env)
    assert values["POSTGRES_PASSWORD"] == "existing-secret"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,}", values["APP_UI_TOKEN"])
    assert values["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.dev.yml"
    assert env.stat().st_mode & 0o777 == 0o600
    assert values["APP_UI_TOKEN"] in first.stdout  # printed once in the summary
    # Second run: nothing missing, nothing written, no new backup.
    second = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert second.returncode == 0, second.stderr
    assert env.read_text() == text and box.backups() == backups
    assert "not modified" in second.stdout
    assert not [c for c in box.calls() if c.startswith(("docker compose up", "git clone"))]


def test_real_run_creates_env_and_rerun_keeps_secrets_but_follows_mode(box):
    first = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "full", "--sources", "google")
    assert first.returncode == 0, first.stdout + first.stderr
    env = box.repo / ".env"
    assert env.stat().st_mode & 0o777 == 0o600 and not box.backups()
    values = parse_env(env)
    assert_secrets(values)
    assert_fernet(values["GOOGLE_TOKEN_ENCRYPTION_KEY"])
    assert values["COMPOSE_FILE"] == "docker-compose.yml"
    before = env.read_text()
    # A different mode later: secrets stay, COMPOSE_FILE follows, the old file is kept.
    second = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert second.returncode == 0, second.stderr
    (backup,) = box.backups()
    assert backup.read_text() == before
    after = parse_env(env)
    assert after["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.dev.yml"
    for name in ("POSTGRES_PASSWORD", "APP_UI_TOKEN", "GOOGLE_TOKEN_ENCRYPTION_KEY"):
        assert after[name] == values[name]
    assert (
        after["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"]
        == values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"]
    )
    changed = [line for line in env.read_text().splitlines() if line not in before.splitlines()]
    assert changed == ["COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml"]
    # Same choices again: nothing to do, no further backup.
    third = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert third.returncode == 0 and len(box.backups()) == 1 and "not modified" in third.stdout


PREVIEW_MARKER = "# install.sh --dry-run: the .env it would write. Not used by Docker.\n"


def test_dry_run_preview_matches_a_complete_env(box):
    assert box.run(*REAL, "--platform", HOST_PLATFORM).returncode == 0
    result = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert result.returncode == 0, result.stderr
    preview = (box.repo / ".env.dry-run").read_text()
    assert preview == PREVIEW_MARKER + (box.repo / ".env").read_text()
    # The preview is recognised by its first line and may be replaced by the next dry run.
    again = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert again.returncode == 0, again.stderr
    assert (box.repo / ".env.dry-run").read_text() == preview


def test_env_out_option_and_turkish_output(box, tmp_path):
    target = tmp_path / "preview.env"
    result = box.run(*DRY, "--platform", "linux", "--lang", "tr", "--env-out", str(target))
    assert result.returncode == 0, result.stderr
    assert target.is_file() and not (box.repo / ".env.dry-run").exists()
    assert "Özet" in result.stdout and "Arayüz anahtarı" in result.stdout
    refused = box.run(*DRY, "--platform", "linux", "--env-out", str(box.repo / ".env"))
    assert refused.returncode == 2 and not (box.repo / ".env").exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--mode", "bogus"),
        ("--platform", "beos"),
        ("--sources", "google,fitbit"),
        ("--sources", "none,google"),
        ("--backups", "maybe"),
        ("--apple-receiver", "1"),
        ("--lang", "de"),
        ("--unknown-flag",),
        ("--dir",),
        ("--ref", "--evil"),
    ],
)
def test_bad_arguments_exit_2(box, args):
    result = box.run(*DRY, *args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not (box.repo / ".env.dry-run").exists()


def test_help_is_bilingual(box):
    result = box.run("--help")
    assert result.returncode == 0
    assert "Usage / Kullanım" in result.stdout and "--garmin-key-file" in result.stdout


def test_windows_and_mismatched_platforms_exit_2(box):
    windows = box.run(*DRY, "--platform", "windows")
    assert windows.returncode == 2 and "install.ps1" in windows.stdout
    other = "linux" if IS_MAC else "macos"
    mismatch = box.run(*REAL, "--platform", other)
    assert mismatch.returncode == 2
    assert not (box.repo / ".env").exists()


def test_a_source_left_on_keeps_its_mount_and_key_paths_change_only_when_asked(box):
    first_key, second_key = box.home / "first.key", box.home / "second.key"
    first_key.write_text("a\n")
    second_key.write_text("b\n")
    garmin = ("--sources", "garmin,apple", "--apple-receiver", "yes")
    result = box.run(
        *REAL, "--platform", HOST_PLATFORM, *garmin, "--garmin-key-file", str(first_key)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    env = box.repo / ".env"
    values = parse_env(env)
    token = Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"])
    assert token.is_file() and re.fullmatch(r"[A-Za-z0-9_-]{43}", token.read_text().strip())
    assert "Authorization: Bearer " + token.read_text().strip() in result.stdout  # shown once
    full = ":".join(f"docker-compose{part}.yml" for part in ("", ".dev", ".garmin", ".apple"))
    assert values["COMPOSE_FILE"] == full
    # Re-run without choosing them: both stay on, so both mounts stay; nothing is turned off.
    second = box.run(*REAL, "--platform", HOST_PLATFORM, "--sources", "none")
    assert second.returncode == 0, second.stderr
    kept = parse_env(env)
    assert kept["COMPOSE_FILE"] == full
    assert kept["GARMIN_ENABLED"] == kept["APPLE_RECEIVER_ENABLED"] == "true"
    assert "Garmin was not selected but GARMIN_ENABLED is on" in second.stdout
    assert token.read_text() == Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"]).read_text()
    # Garmin again without the flag: the stored key path is used, not a default.
    third = box.run(*REAL, "--platform", HOST_PLATFORM, *garmin)
    assert third.returncode == 0, third.stderr
    assert parse_env(env)["GARMIN_API_KEY_HOST_FILE"] == str(first_key.resolve())
    backups = box.backups()
    # An explicit --garmin-key-file replaces the path, after a backup.
    fourth = box.run(
        *REAL, "--platform", HOST_PLATFORM, *garmin, "--garmin-key-file", str(second_key)
    )
    assert fourth.returncode == 0, fourth.stderr
    assert parse_env(env)["GARMIN_API_KEY_HOST_FILE"] == str(second_key.resolve())
    assert len(box.backups()) == len(backups) + 1
    assert parse_env(box.backups()[-1])["GARMIN_API_KEY_HOST_FILE"] == str(first_key.resolve())
    for name in ("POSTGRES_PASSWORD", "APP_UI_TOKEN", "APPLE_RECEIVER_TOKEN_HOST_FILE"):
        assert parse_env(env)[name] == values[name]


def test_missing_garmin_key_continues_with_garmin_off(box):
    result = box.run(*REAL, "--platform", HOST_PLATFORM, "--sources", "google,garmin")
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    assert values["GARMIN_ENABLED"] == "false"
    assert values["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.dev.yml"
    assert "GARMIN_API_KEY_HOST_FILE" not in values
    assert_fernet(values["GOOGLE_TOKEN_ENCRYPTION_KEY"])  # the other sources still apply
    assert "Garmin is off" in result.stdout and "--sources garmin" in result.stdout
    assert ".config/garmin/connect-personal-api-key" in result.stdout


def test_32_bit_arm_is_refused(box):
    box.stub(
        "uname",
        '#!/bin/sh\ncase "$1" in -m) echo armv7l ;; -s) echo Linux ;; *) echo Linux ;; esac\n',
    )
    preview = box.run(*DRY, "--platform", "raspberrypi")
    assert preview.returncode == 0 and "32-bit" in preview.stdout
    real = box.run(*REAL, "--platform", "raspberrypi")
    assert real.returncode == 2 and "32-bit" in real.stdout
    assert not (box.repo / ".env").exists()


def test_outside_a_checkout_dry_run_plans_a_clone(box, tmp_path):
    target = tmp_path / "fresh"
    script = tmp_path / "standalone" / "install.sh"
    script.parent.mkdir()
    shutil.copy2(box.repo / "install.sh", script)
    result = subprocess.run(
        [BASH, str(script), *DRY, "--platform", HOST_PLATFORM, "--dir", str(target)],
        cwd=tmp_path,
        env=box.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git clone https://github.com/M3rgen/HealtOS.git" in result.stdout
    assert not target.exists() and not [c for c in box.calls() if c.startswith("git clone")]
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "notes.txt").write_text("mine")
    refused = box.run(*DRY, "--platform", HOST_PLATFORM, "--dir", str(occupied))
    assert refused.returncode == 2 and (occupied / "notes.txt").read_text() == "mine"


def test_launchd_templates_are_valid_plists():
    for name in ("org.healtos.backup.plist", "org.healtos.bridge.plist"):
        data = plistlib.loads((ROOT / "deploy" / "launchd" / name).read_bytes())
        assert data["Label"] == name.removesuffix(".plist")
        assert data["WorkingDirectory"] == "@HEALTOS_DIR@"
        assert data["EnvironmentVariables"]["PATH"] == "@PATH@"
        assert data["Umask"] == 0o077
    backup = plistlib.loads((ROOT / "deploy/launchd/org.healtos.backup.plist").read_bytes())
    assert backup["ProgramArguments"] == ["/bin/bash", "@HEALTOS_DIR@/scripts/backup.sh"]
    assert backup["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert backup["EnvironmentVariables"]["BACKUP_KEY_FILE"] == "@BACKUP_KEY_FILE@"
    bridge = plistlib.loads((ROOT / "deploy/launchd/org.healtos.bridge.plist").read_bytes())
    assert bridge["ProgramArguments"][-1] == "--foreground" and bridge["KeepAlive"] is True


def test_compose_overrides_mount_secrets_read_only():
    import yaml

    for name, target, variable in (
        ("docker-compose.garmin.yml", "/run/secrets/garmin-personal-api-key", "GARMIN_API_KEY"),
        ("docker-compose.apple.yml", "/run/secrets/apple-receiver-token", "APPLE_RECEIVER_TOKEN"),
    ):
        app = yaml.safe_load((ROOT / name).read_text())["services"]["app"]
        assert list(yaml.safe_load((ROOT / name).read_text())["services"]) == ["app"]
        (mount,) = app["volumes"]
        assert mount["type"] == "bind" and mount["read_only"] is True
        assert mount["target"] == target and f"{variable}_HOST_FILE:?" in mount["source"]
        assert app["environment"][f"{variable}_FILE"] == target
        assert app["group_add"] == [f"${{{variable}_HOST_GID:-10001}}"]
        assert "ports" not in app


@pytest.mark.skipif(not IS_MAC, reason="launchd agents are installed on macOS only")
def test_real_run_installs_launch_agents_with_absolute_paths(box):
    result = box.run(
        "--non-interactive", "--no-start", "--platform", "macos", "--backups", "yes",
        "--autostart", "no", "--umay", "no",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    plist = box.home / "Library/LaunchAgents/org.healtos.backup.plist"
    data = plistlib.loads(plist.read_bytes())
    repo = str(box.repo.resolve())
    assert data["WorkingDirectory"] == repo
    assert data["ProgramArguments"] == ["/bin/bash", f"{repo}/scripts/backup.sh"]
    key = box.home / "Library/Application Support/HealtOS/backup.key"
    assert data["EnvironmentVariables"]["BACKUP_KEY_FILE"] == str(key)
    assert key.read_text().startswith("AGE-SECRET-KEY-")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    for placeholder in ("@HEALTOS_DIR@", "@PATH@", "@BACKUP_KEY_FILE@", "@LOG_DIR@"):
        assert placeholder not in plist.read_text()
    assert f"launchctl bootstrap gui/{os.getuid()} {plist}" in box.calls()
    assert "COPY THE BACKUP KEY OFF THIS MACHINE" in result.stdout
    # Re-run: the key is never replaced and an identical agent is left alone.
    again = box.run(
        "--non-interactive", "--no-start", "--platform", "macos", "--backups", "yes",
        "--autostart", "no", "--umay", "no",
    )  # fmt: skip
    assert again.returncode == 0, again.stderr
    assert key.read_text().startswith("AGE-SECRET-KEY-") and "up to date" in again.stdout
    assert not list(plist.parent.glob("*.bak-*"))


@pytest.mark.skipif(
    IS_MAC or not Path("/run/systemd/system").is_dir() or os.geteuid() == 0,
    reason="user units are installed on a non-root systemd Linux host",
)
def test_real_run_installs_rootless_user_units(box):
    result = box.run(
        "--non-interactive", "--no-start", "--platform", "linux", "--backups", "yes",
        "--autostart", "no", "--umay", "no",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    units = box.home / ".config/systemd/user"
    service = (units / "healthos-backup.service").read_text()
    repo = str(box.repo.resolve())
    assert f"WorkingDirectory={repo}\n" in service
    assert f"ExecStart=/bin/bash {repo}/scripts/backup.sh\n" in service
    assert f'Environment="BACKUP_KEY_FILE={box.home}/.config/healtos/backup.key"' in service
    for line in ("User=root", "Requires=docker.service", "PrivateTmp=yes", "/opt/healthos"):
        assert line not in service
    assert (units / "healthos-backup.timer").read_text() == (
        ROOT / "deploy/systemd/healthos-backup.timer"
    ).read_text()
    assert "systemctl --user enable --now healthos-backup.timer" in box.calls()
    assert any(call.startswith("loginctl enable-linger") for call in box.calls())


def test_backup_script_prefers_a_python_that_can_publish_metadata(tmp_path):
    """macOS ships Python 3.9 as python3: backup.sh must pick a 3.11+ interpreter instead."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "python.log"

    def tool(name: str, text: str) -> None:
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)

    tool("docker", "#!/bin/sh\necho PGDMP-synthetic\n")
    tool("age", "#!/bin/sh\ncat\n")
    # An old python3: answers the version probe with "too old", logs anything else.
    tool(
        "python3",
        f'#!/bin/sh\nif [ "$1" = -c ]; then exit 1; fi\necho "old $*" >> "{log}"\n'
        f'exec "{sys.executable}" "$@"\n',
    )
    tool("python3.12", f'#!/bin/sh\necho "new $*" >> "{log}"\nexec "{sys.executable}" "$@"\n')
    key = tmp_path / "backup.key"
    key.write_text("AGE-SECRET-KEY-1SYNTHETIC\n")
    key.chmod(0o600)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "BACKUP_HOST_DIR": str(tmp_path / "backups"),
        "BACKUP_STATUS_HOST_DIR": str(tmp_path / "metadata"),
        "BACKUP_KEY_FILE": str(key),
        "BACKUP_MIN_FREE_MB": "0",
    }
    result = subprocess.run(
        [BASH, str(ROOT / "scripts" / "backup.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert not [call for call in calls if call.startswith("old ")]
    assert any("write_backup_status.py" in call for call in calls)
    assert any("backup_files publish" in call for call in calls)
    assert (tmp_path / "metadata" / "status.json").is_file()
    assert Path(result.stdout.strip()).read_bytes() == b"PGDMP-synthetic\n"


# ---- review fixes: --env-out (D2) ---------------------------------------------------------


def test_env_out_never_replaces_a_real_file(box, tmp_path):
    env = box.repo / ".env"
    env.write_bytes(b"POSTGRES_PASSWORD=keep-this\nAPP_UI_TOKEN=\n")
    env.chmod(0o600)
    (box.repo / ".env.bak-20260101T000000Z").write_bytes(b"POSTGRES_PASSWORD=older\n")
    before = box.files()
    link = tmp_path / "link"
    link.symlink_to(box.repo)
    alias = tmp_path / "alias.env"
    alias.symlink_to(env)
    notes = tmp_path / "notes.txt"
    notes.write_text("mine")
    refused = (
        "./.env",
        ".env",
        str(env),
        str(link / ".env"),
        str(alias),
        "./.env.bak-20260101T000000Z",
        "./.env.bak-new",
        str(notes),
        str(box.repo),
        str(tmp_path / "missing-dir" / "preview.env"),
    )
    for target in refused:
        result = box.run(*DRY, "--platform", HOST_PLATFORM, "--env-out", target)
        assert result.returncode == 2, (target, result.stdout + result.stderr)
        assert box.files() == before, target
        assert notes.read_text() == "mine" and not (tmp_path / "missing-dir").exists()
    existing = box.run(*DRY, "--platform", HOST_PLATFORM, "--env-out", str(notes))
    assert "is not an earlier dry-run preview" in existing.stderr
    # --env-out belongs to --dry-run only.
    real = box.run(*REAL, "--platform", HOST_PLATFORM, "--env-out", str(tmp_path / "x.env"))
    assert real.returncode == 2 and "--env-out only works with --dry-run" in real.stderr
    assert box.files() == before and not (tmp_path / "x.env").exists()


def test_env_out_replaces_only_an_earlier_preview(box, tmp_path):
    target = tmp_path / "preview.env"
    target.write_text(
        "# install.ps1 -DryRun: the .env it would write. Not used by Docker.\nOLD=1\n"
    )
    result = box.run(*DRY, "--platform", HOST_PLATFORM, "--env-out", str(target))
    assert result.returncode == 0, result.stdout + result.stderr
    text = target.read_text()
    assert text.startswith(PREVIEW_MARKER) and "OLD=1" not in text
    assert target.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if ".tmp." in p.name]


# ---- review fixes: COMPOSE_FILE and the default mode (D3, D4) -----------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("full", "docker-compose.yml:first.yml:second.yml"),
        ("trial", "docker-compose.yml:docker-compose.dev.yml:first.yml:second.yml"),
    ],
)
def test_compose_file_keeps_the_users_files_in_order(box, mode, expected):
    (box.repo / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml:first.yml:./docker-compose.dev.yml:second.yml"
        ":docker-compose.windows.yml:first.yml\n"
    )
    result = box.run(*DRY, "--platform", HOST_PLATFORM, "--mode", mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_env(box.repo / ".env.dry-run")["COMPOSE_FILE"] == expected


def test_compose_file_names_the_implicit_override(box):
    (box.repo / "docker-compose.override.yml").write_text("services: {}\n")
    fresh = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert fresh.returncode == 0, fresh.stderr
    expected = "docker-compose.yml:docker-compose.dev.yml:docker-compose.override.yml"
    assert parse_env(box.repo / ".env.dry-run")["COMPOSE_FILE"] == expected
    (box.repo / ".env").write_text("POSTGRES_PASSWORD=keep\nCOMPOSE_FILE=\n")
    unset = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert unset.returncode == 0, unset.stderr
    assert parse_env(box.repo / ".env.dry-run")["COMPOSE_FILE"] == expected
    # A Windows-style separator is read; the Unix installer writes ':'.
    (box.repo / ".env").write_text(
        "COMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE=docker-compose.yml;mine.yml\n"
    )
    windows = box.run(*DRY, "--platform", HOST_PLATFORM)
    assert windows.returncode == 0, windows.stderr
    values = parse_env(box.repo / ".env.dry-run")
    assert values["COMPOSE_FILE"] == "docker-compose.yml:mine.yml"  # full: no dev override
    assert values["COMPOSE_PATH_SEPARATOR"] == ":"


def test_rerun_without_mode_keeps_the_mode_of_the_existing_env(box):
    first = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "full", "--sources", "google")
    assert first.returncode == 0, first.stdout + first.stderr
    env = box.repo / ".env"
    text = env.read_text()
    assert parse_env(env)["COMPOSE_FILE"] == "docker-compose.yml"
    again = box.run(
        "--yes", "--no-start", "--backups", "no", "--autostart", "no", "--umay", "no",
        "--platform", HOST_PLATFORM,
    )  # fmt: skip
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Mode: full" in again.stdout and "not modified" in again.stdout
    assert env.read_text() == text and not box.backups()
    # A trial .env stays trial; an explicit --mode always wins.
    trial = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert trial.returncode == 0 and "Mode: trial" in trial.stdout
    kept = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert kept.returncode == 0 and "Mode: trial" in kept.stdout and "not modified" in kept.stdout
    assert parse_env(env)["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.dev.yml"


# ---- review fixes: `export KEY=value` and enable flags (D10, D11) -------------------------


def test_export_lines_count_as_present(box):
    env = box.repo / ".env"
    env.write_text(
        "export POSTGRES_PASSWORD=keep-this-database-password\n"
        "export APP_UI_TOKEN=\n"
        "export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml\n"
        "LOG_LEVEL=INFO\n"
    )
    env.chmod(0o600)
    result = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = env.read_text().splitlines()
    for key in ("POSTGRES_PASSWORD", "APP_UI_TOKEN", "COMPOSE_FILE"):
        assert len([line for line in lines if f"{key}=" in line]) == 1, key
    assert lines[0] == "export POSTGRES_PASSWORD=keep-this-database-password"
    assert re.fullmatch(r"export APP_UI_TOKEN=[A-Za-z0-9_-]{43,}", lines[1])
    assert lines[2] == "export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml"
    assert "Mode: trial" in result.stdout
    again = box.run(*REAL, "--platform", HOST_PLATFORM)
    assert again.returncode == 0 and "not modified" in again.stdout


def test_env_readers_accept_export_lines(box):
    (box.repo / ".env").write_text(
        "export BACKUP_HOST_DIR=/srv/first\nexport   BACKUP_HOST_DIR='/srv/backups'\n"
        "#export BACKUP_HOST_DIR=/commented\nexported_KEY=no\n"
    )
    installer = box.source(f'env_get "{box.repo}/.env" BACKUP_HOST_DIR')
    assert installer.stdout == "/srv/backups\n", installer.stderr
    reader = subprocess.run(
        [
            BASH, "-c",
            f'eval "$(sed -n "/^env_file_value()/,/^}}/p" "{ROOT}/scripts/backup.sh")"; '
            "env_file_value BACKUP_HOST_DIR; env_file_value KEY",
        ],
        cwd=box.repo, capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert reader.stdout == "/srv/backups\n", reader.stderr


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("", "true"),
        ("false", "true"),
        ("FALSE", "true"),
        ("0", "true"),
        ("No", "true"),
        ("off", "true"),
        ("true", "true"),
        ("TRUE", "TRUE"),
        ("1", "1"),
        ("yes", "yes"),
        ("On", "On"),
    ],
)
def test_enable_flags_flip_only_from_off_values(box, current, expected):
    key = box.home / "garmin.key"
    key.write_text("synthetic\n")
    (box.repo / ".env").write_text(f"GARMIN_ENABLED={current}\n")
    result = box.run(
        *DRY, "--platform", HOST_PLATFORM, "--sources", "garmin", "--garmin-key-file", str(key)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_env(box.repo / ".env.dry-run")["GARMIN_ENABLED"] == expected


# ---- review fixes: explicit Garmin key file (D13) ------------------------------------------


def test_explicit_missing_garmin_key_file_exits_2(box):
    missing = box.home / "nowhere" / "garmin.key"
    for args in (DRY, REAL):
        result = box.run(*args, "--platform", HOST_PLATFORM, "--garmin-key-file", str(missing))
        assert result.returncode == 2, result.stdout + result.stderr
        assert str(missing) in result.stderr
        assert "not found" in result.stderr and "bulunamadı" in result.stderr
        assert not (box.repo / ".env").exists() and not (box.repo / ".env.dry-run").exists()


# ---- review fixes: secret files on Linux (D8) ----------------------------------------------


def test_linux_root_gives_secret_files_to_the_app_group(box):
    box.stub("uname", LINUX_UNAME)
    box.stub("chgrp", STUB.replace("{name}", "chgrp"))
    box.env.update(FAKE_UID="0", FAKE_USER="root")
    key = box.home / "garmin.key"
    key.write_text("synthetic\n")
    key.chmod(0o600)
    result = box.run(
        *REAL, "--platform", "linux", "--sources", "garmin", "--garmin-key-file", str(key),
        "--apple-receiver", "yes",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    assert values["GARMIN_ENABLED"] == values["APPLE_RECEIVER_ENABLED"] == "true"
    assert values["GARMIN_API_KEY_HOST_GID"] == values["APPLE_RECEIVER_TOKEN_HOST_GID"] == "10001"
    token = Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"])
    for path in (key.resolve(), token):
        assert f"chgrp 10001 {path}" in box.calls()
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_linux_shared_primary_group_leaves_the_source_off(box):
    box.stub("uname", LINUX_UNAME)
    box.env.update(FAKE_GID="100", GETENT_GROUP="users:x:100:someone-else")
    key = box.home / "garmin.key"
    key.write_text("synthetic\n")
    key.chmod(0o600)
    (box.repo / ".env").write_text("GARMIN_ENABLED=false\n")
    result = box.run(
        *REAL, "--platform", "linux", "--sources", "garmin", "--garmin-key-file", str(key),
        "--apple-receiver", "yes",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    assert values["GARMIN_ENABLED"] == "false"  # untouched
    assert "APPLE_RECEIVER_ENABLED" not in values and "GARMIN_API_KEY_HOST_FILE" not in values
    assert values["COMPOSE_FILE"] == "docker-compose.yml:docker-compose.dev.yml"
    token = box.home / ".config/healthos/apple-receiver-token"
    out = " ".join(result.stdout.split())
    for path in (key.resolve(), token):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # never widened
        assert f"sudo chgrp 10001 {path} && sudo chmod 640 {path}" in out
    assert "chmod g+r" not in result.stdout and "Authorization: Bearer" not in result.stdout


def test_gid_is_shared(box):
    def shared(gid: str, **env: str) -> bool:
        result = box.source(f"gid_is_shared '{gid}'", FAKE_USER="me", **env)
        assert result.returncode in (0, 1), result.stderr
        return result.returncode == 0

    assert shared("100") and shared("0") and shared("")
    assert not shared("4321")
    assert not shared("4321", GETENT_GROUP="me:x:4321:me", GETENT_PASSWD="me:x:4321:4321::/:")
    assert shared("4321", GETENT_GROUP="team:x:4321:me,other")
    assert shared("4321", GETENT_PASSWD="other:x:4322:4321::/home/other:/bin/sh")


# ---- review fixes: existing directories keep their mode (D12) ------------------------------


def test_existing_key_directory_keeps_its_mode(box, tmp_path):
    existing = tmp_path / "etc-healthos"
    existing.mkdir()
    existing.chmod(0o755)
    made = tmp_path / "new" / "healtos"
    snippet = (
        'HOST_OS="$(uname -s)"; DRY_RUN=0; INTERACTIVE=0; '
        f'make_backup_key "{existing}/backup.key" && make_backup_key "{made}/backup.key"'
    )
    result = box.source(snippet)
    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    for key in (existing / "backup.key", made / "backup.key"):
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # The bridge's /etc/healthos is created private only when it is missing.
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if "install -d -m 700 /etc/healthos" in line]
    assert lines and all("[ -d /etc/healthos ] ||" in line for line in lines)
    assert "chmod 700" not in text


@pytest.mark.parametrize(
    ("flag", "mounted"),
    [("yes", True), ("1", True), ("On", True), ("TRUE", True), ("off", False), ("No", False)],
)
def test_a_source_counts_as_on_unless_its_flag_is_off(box, flag, mounted):
    key = box.home / "garmin.key"
    key.write_text("synthetic\n")
    (box.repo / ".env").write_text(
        f"GARMIN_ENABLED={flag}\nGARMIN_API_KEY_HOST_FILE={key}\n"
        "COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml\n"
    )
    result = box.run(*DRY, "--platform", HOST_PLATFORM, "--sources", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env.dry-run")
    assert ("docker-compose.garmin.yml" in values["COMPOSE_FILE"]) is mounted
    assert values["GARMIN_ENABLED"] == flag  # never switched off or rewritten
    assert ("GARMIN_ENABLED is on" in result.stdout) is mounted


def test_a_replaced_export_line_keeps_its_prefix(box):
    env = box.repo / ".env"
    env.write_text(
        "export POSTGRES_PASSWORD=keep\nexport COMPOSE_FILE=docker-compose.yml:mine.yml\n"
        "  export   GARMIN_ENABLED=false\n"
    )
    env.chmod(0o600)
    key = box.home / "garmin.key"
    key.write_text("synthetic\n")
    result = box.run(
        *REAL, "--platform", HOST_PLATFORM, "--mode", "trial", "--sources", "garmin",
        "--garmin-key-file", str(key),
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    lines = env.read_text().splitlines()
    files = "docker-compose.yml:docker-compose.dev.yml:docker-compose.garmin.yml:mine.yml"
    assert lines[:3] == [
        "export POSTGRES_PASSWORD=keep",
        f"export COMPOSE_FILE={files}",
        "export GARMIN_ENABLED=true",
    ]
    for name in ("COMPOSE_FILE", "GARMIN_ENABLED"):
        assert len([line for line in lines if f"{name}=" in line]) == 1, name
