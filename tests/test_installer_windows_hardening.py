"""install.ps1 hardening: secrets on the console, Google-less full mode, release tag, strict mode
and .env backups outside the checkout (IP-11/4, IP-2, IP-8/1, IP-8/3, IP-8/5).

Runs PowerShell like tests/test_installer_windows.py: -DryRun against a temporary checkout, or
the installer's functions loaded on their own, with HOME/APPDATA/LOCALAPPDATA in tmp_path.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_installer_windows import (
    INSTALLER,
    IS_WINDOWS,
    PWSH,
    WINDOWS_DIR,
    code_only,
    dry_run,
    env_values,
    mode_of,
    needs_pwsh,
    pwsh,
    text,
)
from test_installer_windows import checkout as checkout
from test_installer_windows import home as home

VERSION = re.search(
    r'^\$HEALTHOS_INSTALLER_VERSION = "([^"]+)"$', text(INSTALLER), re.MULTILINE
).group(1)
TAG = f"v{VERSION}"
UI_TOKEN = "synthetic-ui-token-0123456789abcdefghijklmnopq"
APPLE_NEW = (
    "$script:AppleReceiverChoice = 'yes'\n$script:AppleTokenNew = 'synthetic-apple-token-value'\n"
)
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX paths and mode bits")

# Stand-ins for the installer's parameters, then its own state, then strict mode: the functions
# run exactly as in the real script.
STRICT_PRELUDE = """
$Lang = ''; $Platform = ''; $Mode = ''; $Sources = ''; $Dir = ''; $Ref = ''
$GarminKeyFile = ''; $GoogleClientId = ''; $GoogleClientSecret = ''; $AppleReceiver = ''
$Backups = ''; $Autostart = ''; $Umay = ''; $EnvOut = ''
$NonInteractive = $true; $DryRun = $false; $NoStart = $true; $Help = $false
Initialize-InstallerState
Set-StrictMode -Version Latest
"""


def write_strict(tmp_path: Path, body: str, lang: str = "en") -> Path:
    """A script running `body` with every function and top-level $HealthOS* value of
    install.ps1 loaded, under strict mode."""
    script = tmp_path / "strict.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{WINDOWS_DIR / 'common.ps1'}'\n"
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALLER}', [ref]$null, [ref]$null)\n"
        "foreach ($node in $ast.EndBlock.Statements) {\n"
        "    $t = $node.Extent.Text\n"
        "    if ($node -is [System.Management.Automation.Language.FunctionDefinitionAst] -or\n"
        "        $t -like '$HealthOS*=*' -or $t -like '$HEALTHOS_INSTALLER_VERSION =*') {\n"
        "        Invoke-Expression $t\n"
        "    }\n"
        "}\n" + STRICT_PRELUDE + f"$script:InstallLang = '{lang}'\n" + body,
        encoding="utf-8",
    )
    return script


def run_strict(tmp_path: Path, env: dict[str, str], body: str, lang: str = "en"):
    return pwsh("-File", str(write_strict(tmp_path, body, lang)), env=env)


def make_repo(tmp_path: Path, env_text: str | None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    shutil.copy(INSTALLER.parent / ".env.example", repo / ".env.example")
    if env_text is not None:
        (repo / ".env").write_text(env_text, encoding="utf-8")
    return repo


def backup_dir(home: dict[str, str]) -> Path:
    if IS_WINDOWS:
        return Path(home["LOCALAPPDATA"]) / "HealthOS" / "env-backups"
    return Path(home["HOME"]) / ".local" / "state" / "healthos" / "env-backups"


def slug(path: Path) -> str:
    return re.sub(r"[:\\/]+", "_", re.sub(r"^[\\/]+", "", str(path))).rstrip("_")


def summary_body(repo: Path, extra: str = "") -> str:
    return (
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n$script:InstallMode = 'full'\n"
        + extra
        + "Show-Summary -Started $false -Missing @()\n"
    )


# ---------------------------------------------------------------------------------------------
# IP-8/3: strict mode, stop on errors, narrow native-command blocks (static)


def test_strict_mode_and_stop_are_set_at_the_top():
    after_param = code_only(INSTALLER).split("\n)\n", 1)[1]
    head = after_param[: after_param.index("\nfunction ")]
    top = [line for line in head.splitlines() if line and not line[0].isspace()]
    assert "Set-StrictMode -Version Latest" in top
    assert "$ErrorActionPreference = 'Stop'" in top
    # The execution-policy bypass stays the first statement (test_installer_windows).
    assert top[0].startswith("try { Set-ExecutionPolicy")


def test_continue_blocks_are_narrow_and_restored_in_finally():
    lines = code_only(INSTALLER).splitlines()
    found = 0
    for index, line in enumerate(lines):
        if "$ErrorActionPreference = 'Continue'" not in line:
            continue
        found += 1
        window = "\n".join(lines[index : index + 8])
        assert "finally" in window, f"line {index}: no finally after the Continue block"
        assert re.search(r"\$ErrorActionPreference = \$previous", window), index
    assert found >= 6


# ---------------------------------------------------------------------------------------------
# IP-11/4: the UI token and a new Apple token only on an interactive console


@needs_pwsh
@pytest.mark.parametrize(
    "lang, hidden",
    [("en", "not printed (no terminal)"), ("tr", "yazdırılmadı (terminal yok)")],
)
def test_ui_token_is_not_printed_when_output_is_redirected(
    tmp_path: Path, home: dict[str, str], lang: str, hidden: str
):
    repo = make_repo(tmp_path, f"APP_UI_TOKEN={UI_TOKEN}\nHEALTHOS_SOURCES=apple\n")
    extra = APPLE_NEW
    result = run_strict(tmp_path, home, summary_body(repo, extra), lang)
    assert result.returncode == 0, result.stdout + result.stderr
    assert UI_TOKEN not in result.stdout and "synthetic-apple-token-value" not in result.stdout
    assert hidden in result.stdout
    assert f'Select-String APP_UI_TOKEN "{repo / ".env"}"' in result.stdout
    header = "Header: Authorization: Bearer <content of" if lang == "en" else "Başlık:"
    assert header in result.stdout


def run_on_pty(args: list[str], env: dict[str, str]) -> str:
    """Runs pwsh with stdout on a pseudo terminal (an interactive console for .NET)."""
    import pty

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [PWSH, "-NoProfile", "-NonInteractive", *args],
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=slave,
        env=env,
    )
    os.close(slave)
    chunks: list[bytes] = []
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        elif process.poll() is not None:
            break
    process.wait(timeout=30)
    os.close(master)
    return b"".join(chunks).decode("utf-8", "replace")


@needs_pwsh
@pytest.mark.skipif(IS_WINDOWS, reason="pseudo terminal")
def test_tokens_are_printed_on_an_interactive_console(tmp_path: Path, home: dict[str, str]):
    repo = make_repo(tmp_path, f"APP_UI_TOKEN={UI_TOKEN}\n")
    extra = APPLE_NEW
    script = write_strict(tmp_path, summary_body(repo, extra))
    output = run_on_pty(["-File", str(script)], home)
    assert UI_TOKEN in output, output
    assert "Authorization: Bearer synthetic-apple-token-value" in output
    assert "not printed" not in output


@needs_pwsh
def test_ci_masks_the_ui_token_before_any_other_line(checkout: Path, home: dict[str, str]):
    env = dict(home, GITHUB_ACTIONS="true")
    result, out = dry_run(checkout, env, "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    token = env_values(out)["APP_UI_TOKEN"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    lines = [line for line in result.stdout.splitlines() if token in line]
    assert lines == [f"::add-mask::{token}"]
    plain = dry_run(checkout, home, "-Sources", "none", "-Backups", "no")[0]
    assert "::add-mask::" not in plain.stdout


@needs_pwsh
def test_ci_masks_a_new_apple_token_when_it_is_created(tmp_path: Path, home: dict[str, str]):
    token_file = tmp_path / "keys" / "apple-receiver-token"
    repo = make_repo(
        tmp_path, f"APPLE_RECEIVER_ENABLED=true\nAPPLE_RECEIVER_TOKEN_HOST_FILE={token_file}\n"
    )
    body = (
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n$script:AppleReceiverChoice = 'yes'\n"
        "Initialize-AppleToken\n$script:InstallMode = 'full'\n"
        "Show-Summary -Started $false -Missing @()\n"
    )
    result = run_strict(tmp_path, dict(home, GITHUB_ACTIONS="true"), body)
    assert result.returncode == 0, result.stdout + result.stderr
    token = token_file.read_text(encoding="utf-8").strip()
    lines = result.stdout.splitlines()
    assert [line for line in lines if token in line] == [f"::add-mask::{token}"]
    mask = lines.index(f"::add-mask::{token}")
    assert mask < next(i for i, line in enumerate(lines) if "Created the Apple" in line)


# ---------------------------------------------------------------------------------------------
# IP-2: full mode by default, Google only as a chosen source


@needs_pwsh
def test_a_new_install_defaults_to_full_without_google(checkout: Path, home: dict[str, str]):
    result, out = dry_run(checkout, home, "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    assert values["COMPOSE_FILE"] == "docker-compose.yml"
    assert values["HEALTHOS_SOURCES"] == "none"
    assert values["GOOGLE_TOKEN_ENCRYPTION_KEY"] == ""
    assert not values.get("GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION")
    assert "Trial mode is only for looking around" not in result.stdout
    # Nothing Google-related is missing: the stack would start.
    assert "is empty" not in result.stdout and "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI" not in (
        result.stdout
    )
    assert "[dry-run] docker compose up -d --build" in result.stdout


@needs_pwsh
@pytest.mark.parametrize(
    "lang, warning",
    [
        (
            "en",
            "Trial mode is only for looking around: do not keep data in it, there are no backups",
        ),
        ("tr", "Deneme kipi yalnız bakıp çıkmak içindir: veri saklamayın, yedek yok"),
    ],
)
def test_trial_mode_warns(checkout: Path, home: dict[str, str], lang: str, warning: str):
    result, out = dry_run(checkout, home, "-Lang", lang, "-Mode", "trial", "-Sources", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert warning in result.stdout
    assert env_values(out)["COMPOSE_FILE"] == "docker-compose.yml;docker-compose.dev.yml"
    # A re-run of a trial install keeps trial and warns again.
    out.unlink()
    (checkout / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml\n", encoding="utf-8"
    )
    again, _ = dry_run(checkout, home, "-Lang", lang, "-Sources", "none")
    assert again.returncode == 0 and warning in again.stdout


@needs_pwsh
@pytest.mark.parametrize(
    "sources, expected",
    [("apple,google", "google,apple"), ("none", "none"), ("garmin,apple", "apple")],
)
def test_fresh_install_writes_the_chosen_sources(
    checkout: Path, home: dict[str, str], sources: str, expected: str
):
    # garmin without a key file is dropped (Garmin stays off), so it is not written either.
    result, out = dry_run(checkout, home, "-Sources", sources, "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(out)["HEALTHOS_SOURCES"] == expected
    assert "stays in HEALTHOS_SOURCES" not in result.stdout


@needs_pwsh
@pytest.mark.parametrize(
    "existing, chosen, expected, kept",
    [
        ("HEALTHOS_SOURCES=garmin\n", "apple", "garmin,apple", ["garmin"]),
        ("HEALTHOS_SOURCES=none\n", "none", "none", []),
        # Before HEALTHOS_SOURCES: Google configured, Garmin switched on with its key file.
        ("COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml\nGOOGLE_CLIENT_ID=synthetic\n"
         "GARMIN_ENABLED=true\nGARMIN_API_KEY_HOST_FILE=/keys/garmin.key\n",
         "none", "google,garmin", ["google", "garmin"]),
        # A full install without the key ran Google; a trial one did not.
        ("COMPOSE_FILE=docker-compose.yml\n", "apple", "google,apple", ["google"]),
        ("COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml\nGARMIN_ENABLED=false\n"
         "GARMIN_API_KEY_HOST_FILE=/keys/garmin.key\n", "none", "none", []),
    ],
)  # fmt: skip
def test_existing_sources_stay_on(
    checkout: Path, home: dict[str, str], existing: str, chosen: str, expected: str, kept: list
):
    (checkout / ".env").write_text("POSTGRES_PASSWORD=kept\n" + existing, encoding="utf-8")
    result, out = dry_run(checkout, home, "-Sources", chosen, "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_values(out)["HEALTHOS_SOURCES"] == expected
    for name in ("google", "garmin", "apple"):
        note = f"{name} was not selected but stays in HEALTHOS_SOURCES"
        assert (note in result.stdout) == (name in kept), name
    if kept:
        turkish, _ = dry_run(checkout, home, "-Lang", "tr", "-Sources", chosen, "-Backups", "no")
        assert f"{kept[0]} seçilmedi ama HEALTHOS_SOURCES içinde kalıyor" in turkish.stdout


@needs_pwsh
def test_full_mode_without_google_writes_no_google_secrets(checkout: Path, home: dict[str, str]):
    result, out = dry_run(checkout, home, "-Mode", "full", "-Sources", "apple", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    values = env_values(out)
    assert values["GOOGLE_TOKEN_ENCRYPTION_KEY"] == ""
    assert values["GOOGLE_CLIENT_ID"] == ""
    assert (
        "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION" not in values
        or not values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"]
    )
    google, out_google = dry_run(checkout, home, "-Mode", "full", "-Sources", "google")
    assert google.returncode == 0, google.stdout + google.stderr
    values = env_values(out_google)
    assert len(values["GOOGLE_TOKEN_ENCRYPTION_KEY"]) == 44
    assert values["GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION"].startswith("Bearer ")
    # Trial with Google: the Google keys, but no webhook authorization.
    trial, out_trial = dry_run(checkout, home, "-Mode", "trial", "-Sources", "google")
    assert trial.returncode == 0, trial.stdout + trial.stderr
    values = env_values(out_trial)
    assert len(values["GOOGLE_TOKEN_ENCRYPTION_KEY"]) == 44
    assert not values.get("GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION")


@needs_pwsh
@pytest.mark.parametrize(
    "env_text, expected",
    [
        ("COMPOSE_FILE=docker-compose.yml\nAPP_UI_TOKEN=x\nHEALTHOS_SOURCES=none\n", []),
        ("COMPOSE_FILE=docker-compose.yml\nAPP_UI_TOKEN=x\nHEALTHOS_SOURCES=garmin,apple\n", []),
        ("COMPOSE_FILE=docker-compose.yml\nAPP_UI_TOKEN=\nHEALTHOS_SOURCES=none\n",
         ["APP_UI_TOKEN is empty"]),
        ("COMPOSE_FILE=docker-compose.yml;docker-compose.dev.yml\nHEALTHOS_SOURCES=google\n", []),
        ("COMPOSE_FILE=docker-compose.yml\nAPP_UI_TOKEN=x\nHEALTHOS_SOURCES=google\n",
         ["GOOGLE_CLIENT_ID is empty", "GOOGLE_CLIENT_SECRET is empty",
          "GOOGLE_TOKEN_ENCRYPTION_KEY is empty", "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION is empty",
          "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI must be https://<your webhook host>"
          "/webhooks/google-health"]),
    ],
)  # fmt: skip
def test_production_guard_needs_google_only_as_a_source(
    tmp_path: Path, home: dict[str, str], env_text: str, expected: list
):
    repo = make_repo(tmp_path, env_text)
    body = (
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n"
        "foreach ($m in (Get-MissingProduction)) { Write-Output ('MISSING=' + $m) }\n"
        "Write-Output 'DONE'\n"
    )
    result = run_strict(tmp_path, home, body)
    assert result.returncode == 0, result.stdout + result.stderr
    found = [line[8:] for line in result.stdout.splitlines() if line.startswith("MISSING=")]
    assert found == expected


def test_google_menu_and_mode_menu_texts():
    source = text(INSTALLER)
    assert (
        "Google Health API v4 (Fitbit, Pixel Watch, Google Health app) - needs an approved "
        "Google OAuth project and a public webhook" in source
    )
    full = source.index("'Full - keep your data: production settings, daily encrypted backups;")
    trial = source.index("'Trial - only to look around and leave: do not keep data in it,")
    assert full < trial  # Full is item 1 (default), Trial item 2
    assert "$list = @('google')" not in source  # Google is never assumed


# ---------------------------------------------------------------------------------------------
# IP-8/1: the release tag by default


@needs_pwsh
@pytest.mark.parametrize("ref, expected", [(None, TAG), ("main", "main")])
def test_bootstrap_clones_the_release_tag_by_default(
    tmp_path: Path, home: dict[str, str], ref: str | None, expected: str
):
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    target = tmp_path / "healthos"
    flag = f"-Ref {ref} " if ref else ""
    script = (
        f"& ([scriptblock]::Create((Get-Content -Raw '{INSTALLER}'))) -DryRun -NonInteractive "
        f"{flag}-Dir '{target}'; 'alive ' + $LASTEXITCODE"
    )
    result = pwsh("-Command", script, env=home, cwd=empty)
    assert f"git clone --branch {expected} https://github.com/M3rgen/HealthOS.git" in result.stdout
    assert "alive 0" in result.stdout, result.stdout + result.stderr
    assert not target.exists()


@needs_pwsh
def test_help_and_hints_name_the_release_tag(checkout: Path, home: dict[str, str]):
    help_text = pwsh("-File", str(INSTALLER), "-Help").stdout
    assert f"release tag\n                               {TAG} of this installer" in help_text
    assert f"/HealthOS/{TAG}/install.ps1" in help_text and "/main/" not in help_text
    result, _ = dry_run(checkout, home, "-Platform", "linux")
    assert result.returncode == 2
    assert f"https://raw.githubusercontent.com/M3rgen/HealthOS/{TAG}/install.sh" in result.stdout
    if not IS_WINDOWS:
        refused = pwsh("-File", str(checkout / "install.ps1"), "-NonInteractive", env=home)
        assert f"/HealthOS/{TAG}/install.sh" in refused.stderr


@needs_pwsh
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_update_hint_follows_the_checkout(tmp_path: Path, home: dict[str, str]):
    repo = make_repo(tmp_path, f"APP_UI_TOKEN={UI_TOKEN}\n")
    plain = run_strict(tmp_path, home, summary_body(repo))
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "Update later: git pull;" in plain.stdout
    # A detached HEAD (a tag checkout): fetch the tags and check out the new one.
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".git" / "HEAD").write_text("0" * 39 + "1\n", encoding="ascii")
    tagged = run_strict(tmp_path, home, summary_body(repo))
    assert tagged.returncode == 0, tagged.stdout + tagged.stderr
    assert "Update later: git fetch --tags; git checkout <new release tag" in tagged.stdout


# ---------------------------------------------------------------------------------------------
# IP-8/5: .env backups outside the checkout


@needs_pwsh
@posix_only
def test_backup_prefix_is_the_checkout_path(tmp_path: Path, home: dict[str, str]):
    body = (
        "$script:RepoDir = '/srv/healthos'\nWrite-Output ('A=' + (Get-EnvBackupPrefix))\n"
        "$script:RepoDir = '/srv/healthos/'\nWrite-Output ('B=' + (Get-EnvBackupPrefix))\n"
        "Write-Output ('DIR=' + (Get-EnvBackupDir))\n"
    )
    result = run_strict(tmp_path, home, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "A=srv_healthos.env.bak-" in result.stdout
    assert "B=srv_healthos.env.bak-" in result.stdout
    assert f"DIR={backup_dir(home)}" in result.stdout


@needs_pwsh
@posix_only
def test_env_backups_go_to_the_private_directory_and_old_ones_are_copied(
    tmp_path: Path, home: dict[str, str]
):
    repo = make_repo(tmp_path, "POSTGRES_PASSWORD=kept\nAPP_UI_TOKEN=\n")
    old_a = repo / ".env.bak-20260101T000000Z"
    old_b = repo / ".env.bak-20260102T000000Z"
    old_a.write_bytes(b"OLD=a\n")
    old_b.write_bytes(b"OLD=b\r\n")
    target = backup_dir(home)
    prefix = slug(repo) + ".env.bak-"
    target.mkdir(parents=True)
    target.chmod(0o755)
    clash = target / (prefix + "20260102T000000Z")
    clash.write_bytes(b"something else\n")  # same name, other bytes: never overwritten
    body = (
        f"$script:RepoDir = '{repo}'\nRead-EnvLines\n"
        "Set-EnvSecret 'APP_UI_TOKEN' 'synthetic-new-token'\nWrite-EnvFile\n"
        "Write-Output ('BACKUP=' + $script:EnvBackup)\n$script:InstallMode = 'full'\n"
        "Show-Summary -Started $false -Missing @()\n"
    )
    result = run_strict(tmp_path, home, body)
    assert result.returncode == 0, result.stdout + result.stderr
    backup = Path(re.search(r"^BACKUP=(.+)$", result.stdout, re.M).group(1))
    assert backup.parent == target and backup.name.startswith(prefix)
    assert backup.read_bytes() == b"POSTGRES_PASSWORD=kept\nAPP_UI_TOKEN=\n"
    assert (target / (prefix + "20260101T000000Z")).read_bytes() == b"OLD=a\n"
    assert clash.read_bytes() == b"something else\n"
    assert (target / (prefix + "20260102T000000Z-2")).read_bytes() == b"OLD=b\r\n"
    # The originals stay where they were, unchanged; nothing new next to .env.
    assert old_a.read_bytes() == b"OLD=a\n" and old_b.read_bytes() == b"OLD=b\r\n"
    assert sorted(p.name for p in repo.glob(".env.bak-*")) == [old_a.name, old_b.name]
    assert mode_of(target) == 0o700
    assert all(mode_of(p) == 0o600 for p in target.iterdir() if p != clash)
    assert f".env backups of this checkout: 4 in {target}" in result.stdout
    assert "The older .env.bak-* copies next to .env are kept" in result.stdout
    # A second run copies nothing again; only its own new backup is added.
    second = body.replace(
        "Set-EnvSecret 'APP_UI_TOKEN' 'synthetic-new-token'",
        "Set-EnvManaged 'HEALTHOS_SOURCES' 'apple'",
    )
    again = run_strict(tmp_path, home, second)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Copied" in again.stdout and str(old_a) not in again.stdout
    assert len([p for p in target.iterdir() if p.name.startswith(prefix)]) == 5
    assert f".env backups of this checkout: 5 in {target}" in again.stdout


@needs_pwsh
def test_dry_run_plans_the_backup_copies_and_writes_none(checkout: Path, home: dict[str, str]):
    (checkout / ".env").write_text("POSTGRES_PASSWORD=kept\n", encoding="utf-8")
    old = checkout / ".env.bak-20260101T000000Z"
    old.write_bytes(b"OLD=1\n")
    result, _ = dry_run(checkout, home, "-Sources", "none", "-Backups", "no")
    assert result.returncode == 0, result.stdout + result.stderr
    target = backup_dir(home)
    assert f"[dry-run] would copy .env to {target}" in result.stdout
    assert ".env.bak-<UTC stamp>" in result.stdout
    assert f"[dry-run] would copy {old} to {target}" in result.stdout
    assert "the original next to .env is kept" in result.stdout
    assert not target.exists()
    assert old.read_bytes() == b"OLD=1\n"


@needs_pwsh
@pytest.mark.parametrize("name", [".env.bak-preview", "srv_healthos.env.bak-20260101T000000Z"])
def test_env_out_refuses_backup_names(checkout: Path, home: dict[str, str], name: str):
    out = checkout.parent / name
    result = pwsh(
        "-File", str(checkout / "install.ps1"), "-NonInteractive", "-DryRun", "-Autostart", "no",
        "-Sources", "none", "-EnvOut", str(out), env=home,
    )  # fmt: skip
    assert result.returncode == 2, result.stdout + result.stderr
    assert ".env.bak-* copy" in result.stderr
    assert not out.exists()


def test_backup_location_is_named_in_messages():
    source = text(INSTALLER)
    assert "Join-Path (Join-Path $env:LOCALAPPDATA 'HealthOS') 'env-backups'" in source
    assert "'.local') 'state') 'healthos') 'env-backups'" in source
    assert r"%LOCALAPPDATA%\HealthOS\env-backups" in source
    assert "the old .env is kept as .env.bak-<stamp>" not in source
