# ruff: noqa: E501  (embedded PowerShell snippets keep their natural line length)
"""Windows installer (install.ps1), scripts/backup.ps1, scripts/restore.ps1 and deploy/windows/.

Static checks run everywhere. Checks that execute PowerShell run when `pwsh` is on PATH
(Windows, and macOS/Linux with PowerShell 7 installed); they use -DryRun, a temporary fake
checkout and fake `docker`/`age` programs, so nothing touches Docker, Task Scheduler or the
real home directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.ps1"
WINDOWS_DIR = ROOT / "deploy" / "windows"
BACKUP = ROOT / "scripts" / "backup.ps1"
RESTORE = ROOT / "scripts" / "restore.ps1"
AUTOSTART = ROOT / "scripts" / "enable-docker-desktop-autostart.ps1"
PS_FILES = sorted([INSTALLER, BACKUP, RESTORE, AUTOSTART, *WINDOWS_DIR.glob("*.ps1")])
PWSH = shutil.which("pwsh")
IS_WINDOWS = sys.platform == "win32"
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh (PowerShell 7) is not installed")
# Built from parts: the marker word must appear in installer scripts only.
MARKER = "HEALTOS_INSTALLER" + "_VERSION"

# install.sh option -> install.ps1 parameter (docs/INSTALL.md, the shared contract).
CONTRACT = {
    "--lang": "Lang",
    "--platform": "Platform",
    "--mode": "Mode",
    "--sources": "Sources",
    "--dir": "Dir",
    "--ref": "Ref",
    "--garmin-key-file": "GarminKeyFile",
    "--google-client-id": "GoogleClientId",
    "--google-client-secret": "GoogleClientSecret",
    "--apple-receiver": "AppleReceiver",
    "--backups": "Backups",
    "--autostart": "Autostart",
    "--umay": "Umay",
    "--non-interactive": "NonInteractive",
    "--dry-run": "DryRun",
    "--env-out": "EnvOut",
    "--no-start": "NoStart",
    "--help": "Help",
}
PLATFORMS = ["linux", "raspberrypi", "nas", "macos", "windows", "vps"]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_only(path: Path) -> str:
    """Script text without <# ... #> blocks and # comment lines."""
    source = re.sub(r"<#.*?#>", "", text(path), flags=re.DOTALL)
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def param_names(path: Path) -> list[str]:
    block = re.search(r"^param\((.*?)^\)", text(path), re.MULTILINE | re.DOTALL)
    assert block, f"{path.name} has no top-level param block"
    return re.findall(r"\$([A-Za-z]+)\s*(?:=|,|$)", block.group(1), re.MULTILINE)


def function_bodies(source: str) -> dict[str, tuple[int, int]]:
    """Top-level `function Name {` spans (start, end offsets), by brace counting."""
    spans: dict[str, tuple[int, int]] = {}
    for match in re.finditer(r"^function ([\w-]+) \{", source, re.MULTILINE):
        depth, index = 0, match.end() - 1
        while True:
            char = source[index]
            depth += char == "{"
            depth -= char == "}"
            index += 1
            if depth == 0:
                break
        spans[match.group(1)] = (match.start(), index)
    return spans


# ---------------------------------------------------------------------------------------------
# Static checks (no PowerShell needed)


def test_windows_files_exist():
    for path in PS_FILES:
        assert path.is_file(), path
    for name in (
        "common.ps1",
        "register-tasks.ps1",
        "run-backup.ps1",
        "start-umay-bridge.ps1",
        "umay_bridge_task.py",
    ):
        assert (WINDOWS_DIR / name).is_file()


@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_scripts_are_ascii_without_bom(path: Path):
    # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page; `irm | iex` must not
    # see a byte order mark before the param block.
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert all(byte < 128 for byte in raw), f"{path.name} contains non-ASCII bytes"


@pytest.mark.parametrize("path", PS_FILES, ids=lambda p: p.name)
def test_scripts_avoid_powershell7_only_syntax(path: Path):
    code = code_only(path)
    for pattern, what in (
        (r"\?\?", "null-coalescing ??"),
        (r"\?\.", "null-conditional ?."),
        (r"&&|\|\|", "pipeline chain operators"),
        (r"-Parallel\b", "ForEach-Object -Parallel"),
        (r"-AsHashtable\b", "ConvertFrom-Json -AsHashtable"),
    ):
        assert not re.search(pattern, code), f"{path.name}: {what} is PowerShell 7 only"


def test_marker_line_and_version():
    source = text(INSTALLER)
    assert re.search(rf'^\${MARKER} = "(\d+\.\d+\.\d+)"$', source, re.MULTILINE)
    version = re.search(rf'^\${MARKER} = "([^"]+)"$', source, re.MULTILINE).group(1)
    project = re.search(r'^version = "([^"]+)"', text(ROOT / "pyproject.toml"), re.MULTILINE)
    assert project and version == project.group(1)
    for path in PS_FILES:
        if path != INSTALLER:
            assert MARKER not in text(path), path.name
    for name in ("docker-compose.windows.yml", ".env.example"):
        assert MARKER not in text(ROOT / name)


def test_parameters_match_the_shared_contract():
    names = param_names(INSTALLER)
    assert sorted(names) == sorted(CONTRACT.values())
    usage = text(INSTALLER)
    for parameter in CONTRACT.values():
        assert f"-{parameter}" in usage
    unix = ROOT / "install.sh"
    if unix.is_file():
        for option in CONTRACT:
            assert option in text(unix), f"install.sh lacks {option}"


def test_platforms_sources_and_hints_are_listed():
    source = text(INSTALLER)
    for platform in PLATFORMS:
        assert f"'{platform}'" in source
    assert "recommended" in source and "Linux server" in source
    for source_name in ("google", "garmin", "apple", "none"):
        assert f"'{source_name}'" in source
    for label in ("Google Health API v4", "Garmin Connect Personal API", "Apple Health"):
        assert label in source
    for hint in (
        "winget install -e --id Docker.DockerDesktop",
        "winget install -e --id Git.Git",
        "winget install -e --id FiloSottile.age",
        "wsl --install",
    ):
        assert hint in source
    assert "Set-EnvManaged 'COMPOSE_PATH_SEPARATOR' ';'" in source
    assert "https://github.com/M3rgen/HealtOS.git" in source


def test_nothing_is_deleted_or_wiped():
    forbidden = re.compile(
        r"Remove-Item|\bdown\s+-v\b|volume\s+(rm|prune)|system\s+prune|\brm\s+-|Clear-Content"
        r"|git\s+(clean|reset)",
        re.IGNORECASE,
    )
    for path in PS_FILES:
        assert not forbidden.search(code_only(path)), path.name
    # Files are deleted only in backup.ps1: its own unfinished temporary files, and old
    # health-* dumps when BACKUP_RETENTION_DAYS opted in.
    for path in PS_FILES:
        if path != BACKUP:
            assert "::Delete(" not in text(path), path.name
    source = text(BACKUP)
    spans = function_bodies(source)
    allowed = [spans["Remove-PartialFile"], spans["Invoke-OptInRetention"]]
    for match in re.finditer(r"::Delete\(", source):
        assert any(start <= match.start() < end for start, end in allowed)
    retention = source[slice(*spans["Invoke-OptInRetention"])]
    assert "if ($Days -le 0) { return }" in retention
    assert "Get-HealthDumps" in retention
    assert r"'^health-.*\.dump(\.age|\.gpg)?$'" in source


def test_installer_env_rules():
    source = text(INSTALLER)
    # .env is copied byte for byte to a new .env.bak-<stamp> (never over an existing file,
    # readable only by you), then replaced atomically.
    assert (
        "Write-PrivateBytes -Path $backup -Bytes ([System.IO.File]::ReadAllBytes($envFile))"
        in source
    )
    assert "$mode = [System.IO.FileMode]::CreateNew" in source
    assert ".bak-$stamp" in source
    # Secrets are generated with .NET, only when missing or empty.
    assert "RandomNumberGenerator" in source
    for secret in ("POSTGRES_PASSWORD", "APP_UI_TOKEN", "GOOGLE_TOKEN_ENCRYPTION_KEY"):
        assert f"Set-EnvSecret '{secret}'" in source
    # Backup key outside the checkout and .env, current user only.
    assert "Join-Path $env:APPDATA 'HealtOS'" in source
    assert "'backup.key'" in source and "Set-HealtOSPrivateAcl" in source
    assert "COPY THE BACKUP KEY OFF THIS MACHINE NOW" in source


def test_backup_script_streams_and_writes_the_status_schema():
    source = text(BACKUP)
    assert "'compose', '--profile', 'backup', 'run', '--rm', '-T', 'backup'" in source
    assert "CopyToAsync" in source and "System.Diagnostics.Process" in text(
        WINDOWS_DIR / "common.ps1"
    )
    for field in (
        "timestamp",
        "result",
        "message",
        "file",
        "size_bytes",
        "free_bytes",
        "required_free_bytes",
        "retention_days",
        "pruned_files",
    ):
        assert f'"{field}": ' in source
    assert "'last-backup-status.json'" in source and "'status.json'" in source
    assert '{"schema_version": 1, "last_attempt": {"result": ' in source
    assert "BACKUP_MIN_FREE_MB" in source and "5 GiB before the first dump" in source


def test_scheduled_tasks_and_autostart():
    tasks = text(WINDOWS_DIR / "register-tasks.ps1")
    assert "-Daily -At '03:30'" in tasks and "-StartWhenAvailable" in tasks
    assert "-AtLogOn" in tasks and "start-umay-bridge.ps1" in tasks and "run-backup.ps1" in tasks
    assert "-LogonType Interactive -RunLevel Limited" in tasks
    assert "start_codex_bridge.py" in text(WINDOWS_DIR / "start-umay-bridge.ps1")
    autostart = text(AUTOSTART)
    assert r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" in autostart
    assert "-DryRun" in autostart or "[switch]$DryRun" in autostart


def test_windows_compose_override_is_inert():
    document = yaml.safe_load(text(ROOT / "docker-compose.windows.yml"))
    assert document == {"services": {}}


# ---------------------------------------------------------------------------------------------
# PowerShell checks


def pwsh(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
        check=False,
        timeout=180,
    )


@pytest.fixture
def home(tmp_path: Path) -> dict[str, str]:
    """Environment whose home, APPDATA and LOCALAPPDATA live in tmp_path."""
    base = tmp_path / "home"
    for name in ("Roaming", "Local"):
        (base / name).mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        HOME=str(base),
        USERPROFILE=str(base),
        APPDATA=str(base / "Roaming"),
        LOCALAPPDATA=str(base / "Local"),
    )
    for name in ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR", "BACKUP_RETENTION_DAYS"):
        env.pop(name, None)
    return env


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A minimal HealtOS checkout copy (the installer's checkout test and its scripts)."""
    target = tmp_path / "checkout"
    (target / "scripts").mkdir(parents=True)
    (target / "deploy").mkdir()
    for name in (
        "install.ps1",
        ".env.example",
        *[p.name for p in ROOT.glob("docker-compose*.yml")],
    ):
        shutil.copy(ROOT / name, target / name)
    for script in ROOT.glob("scripts/*.ps1"):
        shutil.copy(script, target / "scripts" / script.name)
    shutil.copy(ROOT / "scripts" / "backup.sh", target / "scripts" / "backup.sh")
    shutil.copytree(WINDOWS_DIR, target / "deploy" / "windows")
    return target


def dry_run(checkout: Path, env: dict[str, str], *args: str):
    out = checkout.parent / "preview.env"
    result = pwsh(
        "-File",
        str(checkout / "install.ps1"),
        "-NonInteractive",
        "-DryRun",
        "-Autostart",
        "no",
        "-EnvOut",
        str(out),
        *args,
        env=env,
    )
    return result, out


def env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


@needs_pwsh
def test_every_script_parses():
    paths = ",".join(f"'{path}'" for path in PS_FILES)
    script = (
        f"$bad = 0; foreach ($p in @({paths})) {{ $e = $null; "
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$e); "
        'foreach ($x in $e) { Write-Output "$p $x"; $bad++ }; '
        # Windows PowerShell 5.1: Join-Path takes one child (no -AdditionalChildPath).
        "$joins = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] "
        "-and $n.GetCommandName() -eq 'Join-Path' }, $true); "
        "foreach ($j in $joins) { if ($j.CommandElements.Count -gt 3) { "
        'Write-Output "$p line $($j.Extent.StartLineNumber): Join-Path with more than one child"; '
        "$bad++ } } }; exit $bad"
    )
    result = pwsh("-Command", script)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_pwsh
def test_turkish_messages_decode(tmp_path: Path):
    # Every M 'english' 'turkish' call: the Turkish text must be valid for [regex]::Unescape.
    script = r"""
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$null)
$bad = 0
$calls = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'M' }, $true)
foreach ($call in $calls) {
    $parts = @($call.CommandElements | Select-Object -Skip 1)
    if ($parts.Count -ne 2) { Write-Output "line $($call.Extent.StartLineNumber): arguments"; $bad++; continue }
    if ($parts[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $parts[0].Value -match '\\u[0-9a-fA-F]{4}') {
        Write-Output "line $($call.Extent.StartLineNumber): escape in English"; $bad++
    }
    if ($parts[1] -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        try { [void][regex]::Unescape($parts[1].Value) } catch { Write-Output "line $($call.Extent.StartLineNumber): $_"; $bad++ }
    }
}
if ($calls.Count -lt 100) { Write-Output "only $($calls.Count) messages"; $bad++ }
exit $bad
"""
    checker = tmp_path / "check.ps1"
    checker.write_text(script, encoding="utf-8")
    result = pwsh("-File", str(checker), str(INSTALLER))
    assert result.returncode == 0, result.stdout + result.stderr


@needs_pwsh
def test_help_is_bilingual_and_exits_zero():
    result = pwsh("-File", str(INSTALLER), "-Help")
    assert result.returncode == 0, result.stderr
    for parameter in CONTRACT.values():
        assert f"-{parameter}" in result.stdout
    assert "Usage:" in result.stdout and "Kullanım:" in result.stdout
    turkish = pwsh("-File", str(INSTALLER), "-Help", "-Lang", "tr")
    assert "Seçenekler" in turkish.stdout and "Usage:" not in turkish.stdout


@needs_pwsh
@pytest.mark.parametrize(
    "args",
    [
        ["-Bogus", "x"],
        ["-Mode", "production"],
        ["-Platform", "solaris"],
        ["-Lang", "de"],
        ["-Sources", "fitbit"],
        ["-Sources", "none,google"],
        ["-Backups", "maybe"],
        ["-EnvOut", "x.env"],
    ],
)
def test_bad_arguments_exit_2(args: list[str], home: dict[str, str]):
    result = pwsh("-File", str(INSTALLER), "-NonInteractive", *args, env=home)
    assert result.returncode == 2, result.stdout + result.stderr


@needs_pwsh
@pytest.mark.skipif(IS_WINDOWS, reason="checks the refusal on a non-Windows host")
def test_real_run_refuses_a_non_windows_host(checkout: Path, home: dict[str, str]):
    result = pwsh("-File", str(checkout / "install.ps1"), "-NonInteractive", env=home)
    assert result.returncode == 2
    assert "install.sh" in result.stdout + result.stderr
    assert not (checkout / ".env").exists()


@needs_pwsh
@pytest.mark.parametrize("platform", ["linux", "raspberrypi", "nas", "macos", "vps"])
def test_other_platforms_point_to_install_sh(platform: str, checkout: Path, home: dict[str, str]):
    result, out = dry_run(checkout, home, "-Platform", platform)
    assert result.returncode == 2
    assert "install.sh" in result.stdout
    assert not out.exists()


@needs_pwsh
def test_trial_dry_run_writes_only_the_preview(checkout: Path, home: dict[str, str]):
    before = sorted(p.name for p in checkout.iterdir())
    result, out = dry_run(checkout, home, "-Mode", "trial", "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in checkout.iterdir()) == before
    assert not list(Path(home["HOME"]).rglob("*.key"))
    assert "[dry-run]" in result.stdout and "docker compose up -d --build" in result.stdout
    values = env_values(out)
    assert re.fullmatch(r"[0-9a-f]{64}", values["POSTGRES_PASSWORD"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", values["APP_UI_TOKEN"])
    assert values["COMPOSE_FILE"] == "docker-compose.yml;docker-compose.dev.yml"
    assert values["COMPOSE_PATH_SEPARATOR"] == ";"
    assert values["APP_ENV"] == "development"  # from .env.example; Compose sets the mode
    assert values["GOOGLE_TOKEN_ENCRYPTION_KEY"] == ""
    # Everything else is the template, unchanged and in order.
    template = [line for line in text(ROOT / ".env.example").splitlines() if "=" in line]
    preview = out.read_text(encoding="utf-8").splitlines()
    assert preview[0].startswith("# install.ps1 -DryRun")
    assert len([line for line in preview if "=" in line]) >= len(template)


@needs_pwsh
def test_full_dry_run_with_every_source(checkout: Path, home: dict[str, str], tmp_path: Path):
    key = tmp_path / "garmin.key"
    key.write_text("synthetic-not-a-key\n", encoding="utf-8")
    result, out = dry_run(
        checkout,
        home,
        "-Mode",
        "full",
        "-Sources",
        "google,garmin,apple",
        "-GarminKeyFile",
        str(key),
        "-AppleReceiver",
        "yes",
        "-GoogleClientId",
        "synthetic.apps.example",
        "-Backups",
        "yes",
        "-Umay",
        "yes",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    fernet = values["GOOGLE_TOKEN_ENCRYPTION_KEY"]
    assert len(fernet) == 44 and len(base64.urlsafe_b64decode(fernet)) == 32
    assert re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"])
    assert values["GOOGLE_CLIENT_ID"] == "synthetic.apps.example"
    assert values["GARMIN_ENABLED"] == "true"
    assert Path(values["GARMIN_API_KEY_HOST_FILE"]) == key.resolve()
    assert values["APPLE_RECEIVER_ENABLED"] == "true"
    assert values["APPLE_RECEIVER_TOKEN_HOST_FILE"].endswith("apple-receiver-token")
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", values["CODEX_BRIDGE_TOKEN"])
    assert values["COMPOSE_FILE"] == (
        "docker-compose.yml;docker-compose.garmin.yml;docker-compose.apple.yml"
    )
    # Missing production values are listed; nothing would start.
    assert "GOOGLE_CLIENT_SECRET is empty" in result.stdout
    assert "Register-ScheduledTask" in result.stdout and "HealtOS Backup" in result.stdout
    assert "HealtOS Umay bridge" in result.stdout
    assert "COPY THE BACKUP KEY OFF THIS MACHINE NOW" in result.stdout
    assert not (Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"])).exists()


@needs_pwsh
def test_existing_env_is_kept_and_only_filled(checkout: Path, home: dict[str, str]):
    existing = (
        "POSTGRES_PASSWORD=existing-password-not-rotated\n"
        "APP_UI_TOKEN=\n"
        "COMPOSE_PATH_SEPARATOR=:\n"
        "COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml:my-override.yml\n"
        "GARMIN_ENABLED=false\n"
    )
    env_file = checkout / ".env"
    env_file.write_text(existing, encoding="utf-8")
    digest = hashlib.sha256(env_file.read_bytes()).hexdigest()
    result, out = dry_run(checkout, home, "-Mode", "full", "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert hashlib.sha256(env_file.read_bytes()).hexdigest() == digest
    assert not list(checkout.glob(".env.bak-*"))
    assert ".env.bak-<UTC stamp>" in result.stdout
    values = env_values(out)
    assert values["POSTGRES_PASSWORD"] == "existing-password-not-rotated"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", values["APP_UI_TOKEN"])
    # Trial -> full drops the development override and keeps a file the user added.
    assert values["COMPOSE_FILE"] == "docker-compose.yml;my-override.yml"
    assert values["GARMIN_ENABLED"] == "false"


@needs_pwsh
def test_missing_default_garmin_key_continues_with_garmin_off(checkout: Path, home: dict[str, str]):
    # No -GarminKeyFile: the default key file does not exist -> Garmin off, a note, exit 0.
    result, out = dry_run(checkout, home, "-Sources", "garmin", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    assert values["GARMIN_ENABLED"] == "false"
    assert "garmin" not in values["COMPOSE_FILE"]
    assert "Garmin is off" in result.stdout and "-Sources garmin" in result.stdout
    assert "connect-personal-api-key" in result.stdout
    turkish, _ = dry_run(checkout, home, "-Lang", "tr", "-Sources", "garmin")
    assert turkish.returncode == 0
    assert "Garmin kapalı" in turkish.stdout


@needs_pwsh
@pytest.mark.parametrize("lang, expected", [("en", "does not exist"), ("tr", "yok")])
def test_explicit_missing_garmin_key_file_exits_2(
    checkout: Path, home: dict[str, str], tmp_path: Path, lang: str, expected: str
):
    # D13: an explicit -GarminKeyFile is honoured or fails; nothing is written.
    missing = tmp_path / "no-such-garmin.key"
    result, out = dry_run(
        checkout, home, "-Lang", lang, "-Sources", "garmin", "-GarminKeyFile", str(missing)
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert str(missing) in result.stderr and expected in result.stderr
    assert "-GarminKeyFile" in result.stderr
    assert not out.exists()


@needs_pwsh
def test_explicit_garmin_key_file_replaces_the_path(
    checkout: Path, home: dict[str, str], tmp_path: Path
):
    key = tmp_path / "new.key"
    key.write_text("synthetic\n", encoding="utf-8")
    (checkout / ".env").write_text(
        "GARMIN_ENABLED=true\nGARMIN_API_KEY_HOST_FILE=/old/garmin.key\n", encoding="utf-8"
    )
    result, out = dry_run(checkout, home, "-Sources", "garmin", "-GarminKeyFile", str(key))
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    assert Path(values["GARMIN_API_KEY_HOST_FILE"]) == key.resolve()
    assert "docker-compose.garmin.yml" in values["COMPOSE_FILE"]


@needs_pwsh
def test_env_out_never_overwrites_a_foreign_file(checkout: Path, home: dict[str, str]):
    foreign = checkout.parent / "preview.env"
    foreign.write_text("not a preview\n", encoding="utf-8")
    result, _ = dry_run(checkout, home, "-Sources", "none")
    assert result.returncode == 2
    assert foreign.read_text(encoding="utf-8") == "not a preview\n"


@needs_pwsh
def test_bootstrap_dry_run_clones_nothing(tmp_path: Path, home: dict[str, str]):
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    target = tmp_path / "healtos"
    script = (
        f"& ([scriptblock]::Create((Get-Content -Raw '{INSTALLER}'))) -DryRun -NonInteractive "
        f"-Ref v0.2.2 -Dir '{target}'; 'alive ' + $LASTEXITCODE"
    )
    result = pwsh("-Command", script, env=home, cwd=empty)
    assert "git clone --branch v0.2.2 https://github.com/M3rgen/HealtOS.git" in result.stdout
    # The scriptblock form must not close the caller's session.
    assert "alive 0" in result.stdout, result.stdout + result.stderr
    assert not target.exists()
    assert not list(empty.iterdir())


@needs_pwsh
def test_register_tasks_dry_run(tmp_path: Path):
    result = pwsh(
        "-File", str(WINDOWS_DIR / "register-tasks.ps1"), "-Backup", "-Umay", "-DryRun",
        "-RepoDir", str(tmp_path),
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "\\HealtOS\\HealtOS Backup" in result.stdout
    assert "\\HealtOS\\HealtOS Umay bridge" in result.stdout
    assert "-ExecutionPolicy Bypass" in result.stdout


# ---------------------------------------------------------------------------------------------
# Review decisions (execution policy, COMPOSE_FILE, re-run mode, bridge task, private files,
# `export` lines, enable flags)


def test_execution_policy_is_bypassed_for_this_process_first():
    # D1: the first statement after the param block, before anything can load another script.
    after_param = code_only(INSTALLER).split("\n)\n", 1)[1]
    first = next(line for line in after_param.splitlines() if line.strip())
    assert "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force" in first
    assert first.startswith("try {") and "catch" in first  # Group Policy / non-Windows: go on
    source = text(INSTALLER)
    assert "-Scope LocalMachine" not in source and "-Scope CurrentUser" not in source
    # Scheduled tasks and the backup wrapper start their script with -ExecutionPolicy Bypass -File.
    pattern = r"'-ExecutionPolicy',\s*'Bypass',\s*'-File'"
    assert re.search(pattern, text(WINDOWS_DIR / "register-tasks.ps1"))
    assert re.search(pattern, text(WINDOWS_DIR / "run-backup.ps1"))


def test_bridge_task_never_rewrites_env_statically():
    # D5: the sign-in task goes through umay_bridge_task.py, which disables update_env; the
    # launcher itself writes .env only in update_env.
    task = code_only(WINDOWS_DIR / "start-umay-bridge.ps1")
    assert "umay_bridge_task.py" in task and "start_codex_bridge.py" not in task
    assert (
        "Get-HealtOSDotEnvValue -File (Join-Path $root '.env') -Name 'CODEX_BRIDGE_TOKEN'" in task
    )
    assert "EnvironmentVariables['CODEX_BRIDGE_TOKEN'] = $token" in task
    assert "launcher.update_env = keep_env" in text(WINDOWS_DIR / "umay_bridge_task.py")
    launcher = text(ROOT / "scripts" / "start_codex_bridge.py")
    assert "def update_env(token):" in launcher
    writes = [m.start() for m in re.finditer(r"(?<!def )write_private\(\s*path\b", launcher)]
    update_env = launcher.index("def update_env(token):")
    assert writes and all(update_env < at < launcher.index("def supervise(") for at in writes)
    # install.ps1 writes the token and URL once, only when missing or empty.
    source = text(INSTALLER)
    assert "Set-EnvSecret 'CODEX_BRIDGE_TOKEN' (New-UrlSafeSecret)" in source
    assert "Set-EnvSecret 'CODEX_BRIDGE_URL' 'ws://host.docker.internal:8766'" in source


def test_bridge_dependencies_match_install_sh():
    # D7: runtime dependencies only, with the same uv flags as install.sh.
    assert "uv sync --frozen --no-dev" in text(ROOT / "install.sh")
    source = code_only(INSTALLER)
    assert "& $uv sync --frozen --no-dev" in source
    assert not re.search(r"uv sync --frozen(?! --no-dev)", source)
    assert "'run', '--frozen', '--no-dev'," in text(WINDOWS_DIR / "start-umay-bridge.ps1")


def test_bridge_python_range_is_shared_and_matches_the_lock():
    # uv would otherwise pick the newest host Python (Homebrew 3.15), for which uv.lock has no
    # psycopg-binary wheel. One range: install.sh, common.ps1 and the CI installer job.
    match = re.search(r"^BRIDGE_PYTHON='([^']+)'$", text(ROOT / "install.sh"), re.M)
    assert match
    spec = match.group(1)
    assert f"return '{spec}'" in text(WINDOWS_DIR / "common.ps1")
    assert 'run uv sync --frozen --no-dev --python "$BRIDGE_PYTHON"' in text(ROOT / "install.sh")
    source = code_only(INSTALLER)
    assert "& $uv sync --frozen --no-dev --python (Get-HealtOSBridgePython)" in source
    bridge_task = text(WINDOWS_DIR / "start-umay-bridge.ps1")
    assert "'--no-dev', '--python', (Get-HealtOSBridgePython)," in bridge_task
    ci = yaml.safe_load(text(ROOT / ".github" / "workflows" / "ci.yml"))
    steps = ci["jobs"]["installer"]["steps"]
    assert any(f"--python '{spec}'" in step.get("run", "") for step in steps)
    # Every minor the range allows must have a psycopg-binary wheel in uv.lock.
    low, high = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", spec).groups()
    lock = text(ROOT / "uv.lock")
    block = lock[lock.index('[[package]]\nname = "psycopg-binary"') + 1 :]
    block = block[: block.index("[[package]]")]
    for minor in range(int(low), int(high)):
        assert f"cp3{minor}-cp3{minor}-" in block, minor


def fake_bridge_tree(tmp_path: Path, env_text: str) -> Path:
    """A checkout with deploy/windows and a stand-in launcher whose update_env rewrites .env."""
    root = tmp_path / "bridge-checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copytree(WINDOWS_DIR, root / "deploy" / "windows")
    (root / "scripts" / "start_codex_bridge.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def update_env(token):\n"
        "    (ROOT / '.env').write_text('REWRITTEN\\n', encoding='utf-8')\n"
        "def main():\n"
        "    token = os.environ.get('CODEX_BRIDGE_TOKEN', '')\n"
        "    print('bridge token=' + token)\n"
        "    update_env(token)\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(env_text, encoding="utf-8")
    return root


def run_task_launcher(root: Path, env: dict[str, str]):
    return subprocess.run(
        [sys.executable, str(root / "deploy" / "windows" / "umay_bridge_task.py")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def test_bridge_task_launcher_keeps_env(tmp_path: Path):
    root = fake_bridge_tree(tmp_path, "CODEX_BRIDGE_TOKEN=from-env\n")
    env = dict(os.environ, CODEX_BRIDGE_TOKEN="from-env")
    result = run_task_launcher(root, env)
    assert result.returncode == 0, result.stderr
    assert "bridge token=from-env" in result.stdout
    assert text(root / ".env") == "CODEX_BRIDGE_TOKEN=from-env\n"
    env["CODEX_BRIDGE_TOKEN"] = ""
    result = run_task_launcher(root, env)
    assert result.returncode == 1 and "CODEX_BRIDGE_TOKEN is empty" in result.stderr


FAKE_UV = r"""
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
rest = args[args.index("python") + 1:]
sys.exit(subprocess.call([sys.executable, *rest], stdin=subprocess.DEVNULL))
"""


@needs_pwsh
def test_bridge_task_passes_the_env_token_and_never_rewrites_env(
    tmp_path: Path, home: dict[str, str]
):
    env_text = "export CODEX_BRIDGE_TOKEN='token-from-dotenv'\nCODEX_BRIDGE_URL=ws://example\n"
    root = fake_bridge_tree(tmp_path, env_text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fake(bin_dir, "uv", FAKE_UV)
    env = dict(home)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        FAKE_UV_LOG=str(tmp_path / "uv.log"),
        CODEX_BRIDGE_TOKEN="stale-shell-token",
    )
    task = root / "deploy" / "windows" / "start-umay-bridge.ps1"
    result = pwsh("-File", str(task), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert text(root / ".env") == env_text
    assert "run --frozen --no-dev --python >=3.12,<3.15 --project" in text(tmp_path / "uv.log")
    log = next(Path(home["HOME"]).rglob("umay-bridge.log"))
    assert "bridge token=token-from-dotenv" in text(log)
    # No token in .env: the task stops before starting anything.
    (root / ".env").write_text("CODEX_BRIDGE_TOKEN=\n", encoding="utf-8")
    (tmp_path / "uv.log").unlink()
    result = pwsh("-File", str(task), env=env)
    assert result.returncode == 1
    assert "CODEX_BRIDGE_TOKEN is empty" in text(log)
    assert not (tmp_path / "uv.log").exists()
    assert text(root / ".env") == "CODEX_BRIDGE_TOKEN=\n"


@needs_pwsh
def test_umay_dry_run_fills_bridge_values_once(checkout: Path, home: dict[str, str]):
    (checkout / ".env").write_text("POSTGRES_PASSWORD=kept\n", encoding="utf-8")
    result, out = dry_run(checkout, home, "-Sources", "none", "-Backups", "no", "-Umay", "yes")
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", values["CODEX_BRIDGE_TOKEN"])
    assert values["CODEX_BRIDGE_URL"] == "ws://host.docker.internal:8766"
    assert "[dry-run] uv sync --frozen --no-dev" in result.stdout
    (checkout / ".env").write_text(
        "CODEX_BRIDGE_TOKEN=existing\nCODEX_BRIDGE_URL=ws://custom:1\n", encoding="utf-8"
    )
    result, out = dry_run(checkout, home, "-Sources", "none", "-Backups", "no", "-Umay", "yes")
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    assert values["CODEX_BRIDGE_TOKEN"] == "existing"
    assert values["CODEX_BRIDGE_URL"] == "ws://custom:1"


def run_installer_functions(tmp_path: Path, body: str):
    """Runs `body` with every function of install.ps1 (and common.ps1) loaded, main not run."""
    script = tmp_path / "unit.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{WINDOWS_DIR / 'common.ps1'}'\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALLER}', [ref]$null, [ref]$null)\n"
        "foreach ($node in $ast.EndBlock.Statements) {\n"
        "    if ($node -is [System.Management.Automation.Language.FunctionDefinitionAst] -or\n"
        "        $node.Extent.Text -like '$HealtOSManagedCompose =*') { Invoke-Expression $node.Extent.Text }\n"
        "}\n"
        "$script:InstallLang = 'en'\n"
        "$script:Notes = New-Object System.Collections.Generic.List[string]\n" + body,
        encoding="utf-8",
    )
    return pwsh("-File", str(script))


def output_values(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def compose_case(tmp_path: Path, env_text: str | None, mode: str, override: bool = False):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    shutil.copy(ROOT / ".env.example", repo / ".env.example")
    if env_text is not None:
        (repo / ".env").write_text(env_text, encoding="utf-8")
    if override:
        (repo / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    result = run_installer_functions(
        tmp_path,
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n$script:InstallMode = '{mode}'\n"
        "Write-Output ('FILES=' + (Get-ComposeFiles))\n"
        "Write-Output ('DEFAULT=' + (Get-DefaultMode))\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    found = output_values(result.stdout)
    return found["FILES"], found["DEFAULT"]


@needs_pwsh
@pytest.mark.parametrize(
    "env_text, mode, override, expected",
    [
        # D3: unknown entries keep their order; managed ones (also ./-prefixed and the inert
        # windows file) are chosen by the installer.
        (
            "COMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE=mine-a.yml;./docker-compose.yml;"
            "docker-compose.dev.yml;mine-b.yml;docker-compose.windows.yml;"
            "docker-compose.garmin.yml;sub/mine-c.yml\nGARMIN_ENABLED=false\n",
            "full",
            False,
            "docker-compose.yml;mine-a.yml;mine-b.yml;sub/mine-c.yml",
        ),
        # Compose loaded docker-compose.override.yml by itself while COMPOSE_FILE was unset.
        ("POSTGRES_PASSWORD=x\n", "trial", True,
         "docker-compose.yml;docker-compose.dev.yml;docker-compose.override.yml"),
        (None, "full", True, "docker-compose.yml;docker-compose.override.yml"),
        ("COMPOSE_FILE=\n", "trial", True,
         "docker-compose.yml;docker-compose.dev.yml;docker-compose.override.yml"),
        # An explicit COMPOSE_FILE without it is the user's choice.
        ("COMPOSE_FILE=docker-compose.yml\n", "full", True, "docker-compose.yml"),
        ("POSTGRES_PASSWORD=x\n", "trial", False, "docker-compose.yml;docker-compose.dev.yml"),
    ],
)  # fmt: skip
def test_compose_file_keeps_user_entries(tmp_path, env_text, mode, override, expected):
    files, _ = compose_case(tmp_path, env_text, mode, override)
    assert files == expected


@needs_pwsh
@pytest.mark.parametrize(
    "env_text, expected",
    [
        (None, "trial"),
        ("POSTGRES_PASSWORD=x\n", "trial"),
        ("COMPOSE_FILE=\n", "trial"),
        ("COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml\n", "trial"),
        ("COMPOSE_FILE=docker-compose.yml;./docker-compose.dev.yml\n", "trial"),
        ("COMPOSE_PATH_SEPARATOR=:\nCOMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml\n", "trial"),
        ("COMPOSE_FILE=docker-compose.yml\n", "full"),
        ("COMPOSE_FILE=docker-compose.yml;docker-compose.garmin.yml;mine.yml\n", "full"),
        ("export COMPOSE_FILE=docker-compose.yml\n", "full"),
    ],
)  # fmt: skip
def test_default_mode_comes_from_the_existing_env(tmp_path, env_text, expected):
    _, default = compose_case(tmp_path, env_text, "trial")
    assert default == expected


@needs_pwsh
def test_rerun_keeps_full_mode_unless_mode_is_given(checkout: Path, home: dict[str, str]):
    # D4 end to end: a full install stays full on a plain re-run; -Mode always wins.
    (checkout / ".env").write_text(
        "POSTGRES_PASSWORD=kept\nCOMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE=docker-compose.yml\n",
        encoding="utf-8",
    )
    result, out = dry_run(checkout, home, "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "full mode is preselected" in result.stdout
    values = env_values(out)
    assert values["COMPOSE_FILE"] == "docker-compose.yml"
    assert values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"].startswith("Bearer ")
    out.unlink()
    result, out = dry_run(checkout, home, "-Mode", "trial", "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(out)["COMPOSE_FILE"] == "docker-compose.yml;docker-compose.dev.yml"
    out.unlink()
    (checkout / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml\n", encoding="utf-8"
    )
    result, out = dry_run(checkout, home, "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preselected" not in result.stdout
    assert env_values(out)["COMPOSE_FILE"] == "docker-compose.yml;docker-compose.dev.yml"


@needs_pwsh
def test_fresh_install_keeps_an_implicit_override(checkout: Path, home: dict[str, str]):
    (checkout / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    result, out = dry_run(checkout, home, "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(out)["COMPOSE_FILE"] == (
        "docker-compose.yml;docker-compose.dev.yml;docker-compose.override.yml"
    )


@needs_pwsh
def test_export_lines_count_as_present(checkout: Path, home: dict[str, str]):
    # D10: `export KEY=value` is read and never gets a second KEY= line.
    (checkout / ".env").write_text(
        "export POSTGRES_PASSWORD='exported-password'\n"
        "export APP_UI_TOKEN=\n"
        "export COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml;mine.yml\n",
        encoding="utf-8",
    )
    result, out = dry_run(checkout, home, "-Mode", "full", "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    for key in ("POSTGRES_PASSWORD", "APP_UI_TOKEN", "COMPOSE_FILE"):
        assert len([line for line in lines if re.match(rf"^(export )?{key}=", line)]) == 1, key
    assert "export POSTGRES_PASSWORD='exported-password'" in lines
    assert "export COMPOSE_FILE=docker-compose.yml;mine.yml" in lines
    assert any(re.fullmatch(r"export APP_UI_TOKEN=[A-Za-z0-9_-]{43}", line) for line in lines)


@needs_pwsh
def test_backup_settings_read_export_lines(tmp_path: Path):
    dotenv = tmp_path / "dot.env"
    dotenv.write_text(
        'BACKUP_RETENTION_DAYS=1\nexport BACKUP_RETENTION_DAYS="30"\n', encoding="utf-8"
    )
    backup_env = tmp_path / "backup.env"
    backup_env.write_text("export BACKUP_AGE_RECIPIENT=age1synthetic\n", encoding="utf-8")
    script = (
        f". '{WINDOWS_DIR / 'common.ps1'}'; "
        f"Write-Output ('DAYS=' + (Get-HealtOSDotEnvValue -File '{dotenv}' -Name 'BACKUP_RETENTION_DAYS')); "
        f"Import-HealtOSBackupEnv '{backup_env}'; Write-Output ('RECIPIENT=' + $env:BACKUP_AGE_RECIPIENT)"
    )
    env = dict(os.environ)
    env.pop("BACKUP_AGE_RECIPIENT", None)
    result = pwsh("-Command", script, env=env)
    assert "DAYS=30" in result.stdout and "RECIPIENT=age1synthetic" in result.stdout, result.stderr


ME = "S-1-5-21-1000-2000-3000-1001"
OTHER_USER = "S-1-5-21-1000-2000-3000-1002"
SYSTEM, ADMINS, OWNER_RIGHTS = "S-1-5-18", "S-1-5-32-544", "S-1-3-4"


def acl_entry(sid: str, allow: bool = True, read: bool = True, inherit_only: bool = False) -> str:
    flags = {"Allow": allow, "Read": read, "InheritOnly": inherit_only}
    values = "; ".join(f"{key} = ${str(value).lower()}" for key, value in flags.items())
    return f"@{{ Sid = '{sid}'; {values} }}"


@needs_pwsh
@pytest.mark.parametrize(
    "owner, entries, expected",
    [
        # What Set-HealtOSPrivateAcl (protect-file.ps1, Write-PrivateBytes) writes.
        (ME, [acl_entry(ME)], ""),
        # Python's mkdir(mode=0o700) on Windows (pytest's temp dirs): SYSTEM, Administrators,
        # OWNER RIGHTS, inherited by every file created inside.
        (ME, [acl_entry(SYSTEM), acl_entry(ADMINS), acl_entry(OWNER_RIGHTS)], ""),
        (ADMINS, [acl_entry(ME), acl_entry(OWNER_RIGHTS)], ""),
        (SYSTEM, [acl_entry(OWNER_RIGHTS)], ""),
        (OTHER_USER, [acl_entry(ME), acl_entry(OWNER_RIGHTS)], OWNER_RIGHTS),
        ("", [acl_entry(OWNER_RIGHTS)], OWNER_RIGHTS),
        (ME, [acl_entry(ME), acl_entry("S-1-1-0")], "S-1-1-0"),  # Everyone
        (ME, [acl_entry("S-1-5-32-545")], "S-1-5-32-545"),  # Users
        (ME, [acl_entry("S-1-5-11")], "S-1-5-11"),  # Authenticated Users
        (ME, [acl_entry(ME), acl_entry(OTHER_USER)], OTHER_USER),
        (ME, [acl_entry("S-1-1-0", allow=False)], ""),
        (ME, [acl_entry("S-1-1-0", read=False)], ""),
        (ME, [acl_entry("S-1-3-0", inherit_only=True)], ""),  # CREATOR OWNER template
        (ME, [], ""),
    ],
)
def test_key_acl_check_trusts_only_me_system_admins_and_trusted_owner(owner, entries, expected):
    script = (
        f". '{WINDOWS_DIR / 'common.ps1'}'; "
        f"$sid = Get-HealtOSAclReader -CurrentSid '{ME}' -OwnerSid '{owner}' "
        f"-Entries @({', '.join(entries)}); "
        "Write-Output ('READER=' + $sid)"
    )
    result = pwsh("-Command", script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == f"READER={expected}"


@needs_pwsh
@pytest.mark.skipif(not IS_WINDOWS, reason="real Windows ACLs")
def test_protect_file_result_passes_the_key_check(tmp_path: Path):
    key = tmp_path / "backup.key"
    key.write_text("AGE-SECRET-KEY-1SYNTHETICNOTAREALKEY\n", encoding="ascii")
    protect = pwsh(
        "-ExecutionPolicy", "Bypass", "-File", str(WINDOWS_DIR / "protect-file.ps1"), str(key)
    )
    assert protect.returncode == 0, protect.stderr
    script = f". '{WINDOWS_DIR / 'common.ps1'}'; Write-Output ('PROBLEM=' + (Get-HealtOSAclProblem '{key}'))"
    result = pwsh("-Command", script)
    assert result.stdout.strip().splitlines()[-1] == "PROBLEM=", result.stderr


@needs_pwsh
@pytest.mark.parametrize(
    "current, expected",
    [
        ("", "true"),
        ("false", "true"),
        ("FALSE", "true"),
        ("0", "true"),
        ("No", "true"),
        ("off", "true"),
        ("true", "true"),
        ("TRUE", "TRUE"),
        ("yes", "yes"),
        ("1", "1"),
        ("on", "on"),
    ],
)
def test_enable_flags_follow_one_rule(tmp_path: Path, current: str, expected: str):
    # D11: flipped to true only when empty or false/0/no/off (any case); other values stay and
    # count as on (the Garmin mount is kept).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text(
        f"GARMIN_ENABLED={current}\nGARMIN_API_KEY_HOST_FILE=/keys/garmin.key\n", encoding="utf-8"
    )
    result = run_installer_functions(
        tmp_path,
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n$script:InstallMode = 'full'\n"
        "Set-EnvEnable 'GARMIN_ENABLED'\n"
        "Write-Output ('VALUE=' + (Get-EnvValue 'GARMIN_ENABLED'))\n"
        "Write-Output ('FILES=' + (Get-ComposeFiles))\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    found = output_values(result.stdout)
    assert found["VALUE"] == expected
    assert "docker-compose.garmin.yml" in found["FILES"]


@needs_pwsh
@pytest.mark.parametrize("current", ["false", "0", "no", "OFF", ""])
def test_off_flags_keep_the_mount_out(tmp_path: Path, current: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text(
        f"GARMIN_ENABLED={current}\nGARMIN_API_KEY_HOST_FILE=/keys/garmin.key\n", encoding="utf-8"
    )
    result = run_installer_functions(
        tmp_path,
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n$script:InstallMode = 'full'\n"
        "Write-Output ('FILES=' + (Get-ComposeFiles))\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output_values(result.stdout)["FILES"] == "docker-compose.yml"


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


@needs_pwsh
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX mode bits stand in for the Windows ACL here")
def test_dry_run_preview_is_private(checkout: Path, home: dict[str, str], tmp_path: Path):
    # D6: the preview holds the same secrets as .env.
    key = tmp_path / "garmin.key"
    key.write_text("synthetic\n", encoding="utf-8")
    key.chmod(0o644)
    (checkout / ".env").write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")
    (checkout / ".env").chmod(0o644)
    result, out = dry_run(
        checkout, home, "-Sources", "garmin", "-GarminKeyFile", str(key), "-Backups", "no"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert mode_of(out) == 0o600
    # A dry run only reports what it would narrow.
    assert f"[dry-run] would make {key} readable only by you" in result.stdout
    assert f"[dry-run] would make {checkout / '.env'} readable only by you" in result.stdout
    assert mode_of(key) == 0o644 and mode_of(checkout / ".env") == 0o644
    # An earlier, looser preview is narrowed before it is filled again.
    out.chmod(0o644)
    result, out = dry_run(checkout, home, "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert mode_of(out) == 0o600


@needs_pwsh
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX mode bits stand in for the Windows ACL here")
def test_env_backup_and_env_are_private(tmp_path: Path):
    # D6 for the real write path (Write-EnvFile without -DryRun): .env.bak-* is a byte-exact
    # private copy, .env stays private after the replace, older copies are narrowed.
    repo = tmp_path / "repo"
    repo.mkdir()
    original = b"POSTGRES_PASSWORD=kept\r\nAPP_UI_TOKEN=\n"
    (repo / ".env").write_bytes(original)
    (repo / ".env").chmod(0o644)
    older = repo / ".env.bak-20260101T000000Z"
    older.write_bytes(b"OLD=1\n")
    older.chmod(0o644)
    result = run_installer_functions(
        tmp_path,
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n"
        "Set-EnvSecret 'APP_UI_TOKEN' 'new-token'\nWrite-EnvFile\n"
        "Write-Output ('BACKUP=' + $script:EnvBackup)\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    backup = Path(output_values(result.stdout)["BACKUP"])
    assert backup.name.startswith(".env.bak-") and backup != older
    assert backup.read_bytes() == original and mode_of(backup) == 0o600
    assert mode_of(repo / ".env") == 0o600
    assert "APP_UI_TOKEN=new-token" in text(repo / ".env")
    assert "POSTGRES_PASSWORD=kept" in text(repo / ".env")
    assert older.read_bytes() == b"OLD=1\n" and mode_of(older) == 0o600
    assert not list(repo.glob(".env.install-*"))


@needs_pwsh
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX mode bits stand in for the Windows ACL here")
def test_new_private_file_is_private_and_never_replaces(tmp_path: Path):
    target = tmp_path / "keys" / "apple-receiver-token"
    result = run_installer_functions(
        tmp_path,
        f"New-PrivateFile -Path '{target}' -Content \"synthetic-token`n\"\n"
        f"try {{ New-PrivateFile -Path '{target}' -Content 'other'; Write-Output 'REPLACED' }} "
        "catch { Write-Output 'REFUSED' }\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" in result.stdout and "REPLACED" not in result.stdout
    assert target.read_text(encoding="utf-8") == "synthetic-token\n"
    assert mode_of(target) == 0o600 and mode_of(target.parent) == 0o700


# ---------------------------------------------------------------------------------------------
# backup.ps1 / restore.ps1 against fake docker and age (binary-exact streaming)

DUMP = bytes(range(256)) * 300 + b"\r\n\x00\x1a\xff end"
FAKE_DOCKER = r"""
import os, sys
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
args = " ".join(sys.argv[1:])
if "--profile backup run --rm -T backup" in args:
    if os.environ.get("FAKE_DOCKER_FAIL"):
        sys.exit(1)
    sys.stdout.buffer.write(bytes(range(256)) * 300 + b"\r\n\x00\x1a\xff end")
elif "pg_restore" in args:
    with open(os.environ["FAKE_RESTORE_SINK"], "wb") as sink:
        sink.write(sys.stdin.buffer.read())
elif "alembic_version" in args:
    print("20261017_0007")
elif "psql" in args:
    sys.stdout.write("manual_measurements\t1\nraw_health_data\t0\n")
"""
# A stand-in for age: a fixed header, then the bytes unchanged (enough to prove the stream is
# carried byte for byte; the real tool is used when installed, see below).
FAKE_AGE = r"""
import sys
args = sys.argv[1:]
if "--encrypt" in args:
    sys.stdout.buffer.write(b"FAKE-AGE\n" + sys.stdin.buffer.read())
elif "--decrypt" in args:
    data = open(args[-1], "rb").read()
    assert data.startswith(b"FAKE-AGE\n")
    sys.stdout.buffer.write(data[len(b"FAKE-AGE\n"):])
else:
    sys.exit(2)
"""


def install_fake(bin_dir: Path, name: str, body: str) -> None:
    script = bin_dir / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if IS_WINDOWS:
        (bin_dir / f"{name}.cmd").write_text(
            f'@"{sys.executable}" "{script}" %*\r\n', encoding="ascii"
        )
    else:
        launcher = bin_dir / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path: Path, home: dict[str, str]):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fake(bin_dir, "docker", FAKE_DOCKER)
    install_fake(bin_dir, "age", FAKE_AGE)
    key = tmp_path / "backup.key"
    key.write_text("# synthetic test identity\nAGE-SECRET-KEY-1SYNTHETICNOTAREALKEY\n")
    if not IS_WINDOWS:
        key.chmod(0o600)
    env = dict(home)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        BACKUP_HOST_DIR=str(tmp_path / "backups"),
        BACKUP_STATUS_HOST_DIR=str(tmp_path / "metadata"),
        BACKUP_KEY_FILE=str(key),
        BACKUP_MIN_FREE_MB="1",
        FAKE_DOCKER_LOG=str(tmp_path / "docker.log"),
        FAKE_RESTORE_SINK=str(tmp_path / "restored.dump"),
    )
    return env


def run_backup(env: dict[str, str]):
    return pwsh("-ExecutionPolicy", "Bypass", "-File", str(BACKUP), env=env)


def legacy_status(env: dict[str, str]) -> dict:
    return json.loads((Path(env["BACKUP_HOST_DIR"]) / "last-backup-status.json").read_text())


@needs_pwsh
def test_backup_streams_bytes_exactly_and_restore_round_trips(sandbox):
    result = run_backup(sandbox)
    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert output.parent == Path(sandbox["BACKUP_HOST_DIR"])
    assert re.fullmatch(r"health-\d{8}T\d{6}Z\.dump\.age", output.name)
    assert output.read_bytes() == b"FAKE-AGE\n" + DUMP
    assert not list(output.parent.glob("*.partial"))
    status = legacy_status(sandbox)
    assert list(status) == [
        "timestamp",
        "result",
        "message",
        "file",
        "size_bytes",
        "free_bytes",
        "required_free_bytes",
        "retention_days",
        "pruned_files",
    ]
    assert status["result"] == "success" and status["file"] == str(output)
    assert status["size_bytes"] == output.stat().st_size
    assert status["retention_days"] == "never" and status["pruned_files"] == 0
    metadata = json.loads((Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata == {
        "schema_version": 1,
        "last_attempt": {"result": "success", "timestamp": status["timestamp"]},
        "last_success_at": status["timestamp"],
    }
    from app.services.backup_status import read_backup_status

    assert (
        read_backup_status(Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json")["status"]
        == "available"
    )

    if IS_WINDOWS:
        # psql arguments (newlines, %) cannot pass through a .cmd shim; Windows CI checks
        # the backup stream, POSIX runners the restore.
        return
    restored = pwsh("-File", str(RESTORE), str(output), env=sandbox)
    assert restored.returncode == 0, restored.stderr
    assert Path(sandbox["FAKE_RESTORE_SINK"]).read_bytes() == DUMP
    assert "manual_measurements\t1\t1\tequal" in restored.stdout
    assert "Alembic revision: 20261017_0007" in restored.stdout
    log = Path(sandbox["FAKE_DOCKER_LOG"]).read_text()
    assert "createdb -U health health_restore" in log and "--dbname=health_restore" in log


@needs_pwsh
def test_failed_dump_writes_nothing_and_keeps_the_last_success(sandbox):
    assert run_backup(sandbox).returncode == 0
    first = legacy_status(sandbox)["timestamp"]
    time.sleep(1.1)
    result = run_backup({**sandbox, "FAKE_DOCKER_FAIL": "1"})
    assert result.returncode == 1
    assert "pg_dump or encryption failed" in result.stderr
    backups = Path(sandbox["BACKUP_HOST_DIR"])
    assert len(list(backups.glob("health-*"))) == 1
    assert not list(backups.glob("*.partial"))
    metadata = json.loads((Path(sandbox["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_attempt"]["result"] == "failure"
    assert metadata["last_success_at"] == first


@needs_pwsh
def test_free_space_guard_refuses_before_writing(sandbox):
    result = run_backup({**sandbox, "BACKUP_MIN_FREE_MB": "999999999999"})
    assert result.returncode == 1
    assert "refusing to start" in result.stderr
    assert not list(Path(sandbox["BACKUP_HOST_DIR"]).glob("health-*"))
    assert legacy_status(sandbox)["result"] == "failure"
    bad = run_backup({**sandbox, "BACKUP_MIN_FREE_MB": "lots"})
    assert bad.returncode == 1 and "whole number of MiB" in bad.stderr


def old_dumps(backups: Path, days: int) -> tuple[list[Path], list[Path]]:
    backups.mkdir(parents=True, exist_ok=True)
    old = time.time() - days * 86_400
    dumps = [
        backups / "health-20250101T000000Z.dump.age",
        backups / "health-20250102T000000Z.dump.gpg",
        backups / "health-20250101.dump",
    ]
    others = [backups / "notes.txt", backups / "other-20250101.dump"]
    for path in (*dumps, *others):
        path.write_bytes(b"x")
        os.utime(path, (old, old))
    return dumps, others


@needs_pwsh
@pytest.mark.parametrize("value", [None, "0", "never", "36500", "soon"])
def test_backups_are_kept_unless_retention_opts_in(sandbox, value):
    dumps, others = old_dumps(Path(sandbox["BACKUP_HOST_DIR"]), days=400)
    env = dict(sandbox)
    if value is not None:
        env["BACKUP_RETENTION_DAYS"] = value
    result = run_backup(env)
    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in (*dumps, *others))
    assert legacy_status(sandbox)["pruned_files"] == 0


@needs_pwsh
def test_opt_in_retention_deletes_only_old_health_dumps(sandbox):
    dumps, others = old_dumps(Path(sandbox["BACKUP_HOST_DIR"]), days=40)
    result = run_backup({**sandbox, "BACKUP_RETENTION_DAYS": "30"})
    assert result.returncode == 0, result.stderr
    assert not any(path.exists() for path in dumps)
    assert all(path.exists() for path in others)
    assert Path(result.stdout.strip()).exists()
    status = legacy_status(sandbox)
    assert status["pruned_files"] == 3 and status["retention_days"] == "30"


@needs_pwsh
def test_backup_without_a_key_fails_cleanly(sandbox, tmp_path: Path):
    result = run_backup({**sandbox, "BACKUP_KEY_FILE": str(tmp_path / "missing.key")})
    assert result.returncode == 1
    assert "no backup key" in result.stderr and "keys do not belong in .env" in result.stderr


@needs_pwsh
@pytest.mark.skipif(IS_WINDOWS, reason="POSIX mode bits")
def test_backup_refuses_a_key_readable_by_others(sandbox):
    Path(sandbox["BACKUP_KEY_FILE"]).chmod(0o644)
    result = run_backup(sandbox)
    assert result.returncode == 1
    assert "must be readable only by your account" in result.stderr


@needs_pwsh
def test_restore_usage_and_refusals(sandbox, tmp_path: Path):
    usage = pwsh("-File", str(RESTORE), env=sandbox)
    assert usage.returncode == 2 and "usage:" in usage.stderr
    dump = tmp_path / "health-x.dump.age"
    dump.write_bytes(b"x")
    for target in ("health", "postgres", "Bad-Name"):
        refused = pwsh("-File", str(RESTORE), str(dump), target, env=sandbox)
        assert refused.returncode == 1, target
    assert not Path(sandbox["FAKE_DOCKER_LOG"]).exists()


REAL_AGE = shutil.which("age")
REAL_AGE_KEYGEN = shutil.which("age-keygen")


@needs_pwsh
@pytest.mark.skipif(not (REAL_AGE and REAL_AGE_KEYGEN), reason="age is not installed")
def test_backup_with_real_age_decrypts_to_the_dump(sandbox, tmp_path: Path):
    env = dict(sandbox)
    # Hide the fake age; the real one is found on the normal PATH behind the fake docker.
    for fake in (tmp_path / "bin").glob("age*"):
        fake.rename(fake.with_name("unused-" + fake.name))
    key = tmp_path / "real.key"
    subprocess.run([REAL_AGE_KEYGEN, "-o", str(key)], check=True, capture_output=True)
    if not IS_WINDOWS:
        key.chmod(0o600)
    env["BACKUP_KEY_FILE"] = str(key)
    result = run_backup(env)
    assert result.returncode == 0, result.stderr
    output = Path(result.stdout.strip())
    assert DUMP not in output.read_bytes()
    plain = subprocess.run(
        [REAL_AGE, "--decrypt", "--identity", str(key), str(output)],
        check=True,
        capture_output=True,
    ).stdout
    assert plain == DUMP
