"""install.sh has no `set -e` on purpose (IP-8/4): every critical step stops the installer
itself with `|| die` (exit code 1 and a message) instead. These checks keep it that way.

The static check reads install.sh; the dynamic ones run it in the stub sandbox of
test_installer_unix with a failing `git clone` or `docker compose up`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from test_installer_unix import BASH, HOST_PLATFORM, STUB, Sandbox
from test_installer_unix import box as box
from test_installer_unix import pytestmark as pytestmark  # bash on a Unix host

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "install.sh").read_text(encoding="utf-8")

# Steps whose failure must stop the installer (the call sites, not definitions or messages).
CRITICAL = {
    "clone": r'^\s*run git clone "\$REPO_URL" "\$DIR" </dev/null',
    "checkout of the ref": r'^\s*run git -C "\$DIR" checkout --quiet "\$REF"',
    "clone parent directory": r'^\s*mkdir -p "\$\(dirname "\$DIR"\)"',
    "new .env": r'render_env "\$DIR/\.env\.example" "\$target" new',
    "temporary .env into place": r'^\s*mv -f "\$tmp" "\$dest"',
    ".env backup": r'^\s*copy_private "\$DIR/\.env"',
    "older .env backups": r'^\s*copy_private "\$old"',
    "env-backups directory": r'\(umask 077 && mkdir -p "\$d"\)',
    "Garmin key file": r'printf .*"\$key" >"\$GARMIN_KEY_FILE"',
    "Apple token file": r'printf .*"\$APPLE_TOKEN_NEW" >"\$f"',
    "backup key": r'age-keygen -o "\$key" 2>/dev/null\)',
    "secret file group": r'run chgrp "\$gid" "\$f"',
    "secret file mode": r'^\s*run chmod g\+r "\$f"',
    "compose up": r"^\s*compose up -d --build",
    "unit and agent files": r'^\s*install_file "',
}


def statements(pattern: str) -> list[str]:
    """Each matching line joined with its continuation lines (`\\` endings, `||` lines)."""
    lines = SOURCE.splitlines()
    found = []
    for index, line in enumerate(lines):
        if not re.search(pattern, line):
            continue
        text, i = line, index
        while text.rstrip().endswith("\\") or (
            i + 1 < len(lines) and lines[i + 1].lstrip().startswith(("||", "&&"))
        ):
            i += 1
            text = text.rstrip().removesuffix("\\") + " " + lines[i].strip()
        found.append(text)
    return found


def test_no_set_e_but_nounset_and_pipefail():
    assert not re.search(r"^\s*set -[a-z]*e", SOURCE, re.MULTILINE)
    assert re.search(r"^set -u$", SOURCE, re.MULTILINE)
    assert re.search(r"^set -o pipefail$", SOURCE, re.MULTILINE)


@pytest.mark.parametrize("step", sorted(CRITICAL))
def test_every_critical_step_dies_on_failure(step):
    found = statements(CRITICAL[step])
    assert found, f"{step}: pattern no longer matches install.sh; update CRITICAL"
    for text in found:
        assert re.search(r"\|\|\s*(\{[^}]*)?\b(die [12]|exit 1)\b", text), f"{step}: {text}"
        assert "|| true" not in text, f"{step}: {text}"


def test_render_env_dies_on_every_failure_path():
    body = SOURCE.split("render_env() {", 1)[1].split("\n}\n", 1)[0]
    # Every branch that can fail ends in die: awk, the hard link/mv, the preview guard.
    assert body.count("die ") >= 5
    assert "|| true" not in body


def run_real(box: Sandbox, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(box.repo / "install.sh"), "--non-interactive", "--platform", HOST_PLATFORM,
         "--mode", "trial", "--backups", "no", "--autostart", "no", "--umay", "no", *args],
        cwd=cwd or box.repo, env=box.env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip


def test_a_failed_clone_stops_with_exit_1(box, tmp_path):
    target = tmp_path / "fresh"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = run_real(box, "--dir", str(target), "--no-start", cwd=outside)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "git clone failed" in result.stderr
    assert "Summary" not in result.stdout


def test_a_failed_compose_up_stops_with_exit_1(box):
    failing_up = STUB.replace("{name}", "docker").replace(
        "esac\nexit 0", '  "docker compose up"*) exit 1 ;;\nesac\nexit 0'
    )
    assert '"docker compose up"*) exit 1' in failing_up
    box.stub("docker", failing_up)
    box.stub("curl", "#!/bin/sh\nexit 7\n")  # nothing answers on the health URL
    result = run_real(box)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "docker compose up failed" in result.stderr
    assert "HealthOS is running" not in result.stdout
