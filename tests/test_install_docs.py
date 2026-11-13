"""Install documentation and the installers' release pinning (IP-8/1-2, IP-9/4).

- The installers clone their own release tag by default; `main` only when asked for.
- Every install URL in the docs is pinned to that tag (a `main` URL only with `--ref main`).
- docs/INSTALL.md and docs/INSTALL.tr.md have the same heading structure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from test_installer_unix import BASH, DRY, HOST_PLATFORM, Sandbox
from test_installer_unix import box as box

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(
    r'^HEALTHOS_INSTALLER_VERSION="([^"]+)"$', (ROOT / "install.sh").read_text(), re.MULTILINE
).group(1)
TAG = f"v{VERSION}"
DOCS = ["README.md", "docs/INSTALL.md", "docs/INSTALL.tr.md", "docs/AI_INSTALL.md"]
RAW = re.compile(r"raw\.githubusercontent\.com/M3rgen/HealthOS/([^/\s]+)/")
unix_only = pytest.mark.skipif(
    BASH is None or sys.platform.startswith("win"), reason="bash on a Unix host is required"
)


def lines(name: str) -> list[str]:
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_both_installers_carry_the_same_version():
    ps1 = re.search(
        r'^\$HEALTHOS_INSTALLER_VERSION = "([^"]+)"$',
        (ROOT / "install.ps1").read_text(),
        re.MULTILINE,
    )
    assert ps1 and ps1.group(1) == VERSION
    # The installers clone their own tag: the project version, interim X.Y.Z.N included.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert VERSION == project["version"]
    assert re.fullmatch(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){2,3}", VERSION)


@pytest.mark.parametrize("name", [*DOCS, "install.sh", "install.ps1"])
def test_install_urls_are_pinned_to_the_release_tag(name):
    seen = 0
    for number, line in enumerate(lines(name), 1):
        for ref in RAW.findall(line):
            seen += 1
            if ref == "main":
                # The development version is never the default: it says so on the same line.
                assert "--ref main" in line or "-Ref main" in line, f"{name}:{number}: {line}"
            else:
                assert ref == TAG, f"{name}:{number}: {ref} is not {TAG}"
    if name in DOCS[:3]:
        assert seen, f"{name} has no install command"


def test_readme_main_command_and_agent_prompt_use_the_tag():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    raw = "https://raw.githubusercontent.com/M3rgen/HealthOS"
    assert f"curl -fsSL {raw}/{TAG}/install.sh | bash\n" in readme
    assert f"irm {raw}/{TAG}/install.ps1 | iex\n" in readme
    assert f"{raw}/{TAG}/docs/AI_INSTALL.md" in readme
    assert f"{raw}/main/docs/AI_INSTALL.md" not in readme
    agent = (ROOT / "docs/AI_INSTALL.md").read_text(encoding="utf-8")
    assert f"{raw}/{TAG}/docs/AI_INSTALL.md" in agent


@pytest.mark.parametrize("name", DOCS)
def test_documented_clones_are_pinned(name):
    for line in lines(name):
        if "git clone" in line and "github.com/M3rgen/HealthOS.git" in line:
            assert f"--branch {TAG}" in line, line


def headings(name: str) -> list[int]:
    levels, fenced = [], False
    for line in lines(name):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and (match := re.match(r"(#{1,6}) \S", line)):
            levels.append(len(match.group(1)))
    return levels


def test_install_guides_have_the_same_heading_structure():
    english, turkish = headings("docs/INSTALL.md"), headings("docs/INSTALL.tr.md")
    assert len(english) > 10
    assert english == turkish


@unix_only
def test_install_sh_clones_its_release_tag_unless_main_is_asked_for(box: Sandbox, tmp_path):
    script = tmp_path / "standalone" / "install.sh"
    script.parent.mkdir()
    shutil.copy2(box.repo / "install.sh", script)

    def dry(*extra: str) -> str:
        result = subprocess.run(
            [
                BASH,
                str(script),
                *DRY,
                "--platform",
                HOST_PLATFORM,
                "--dir",
                str(tmp_path / "new"),
                *extra,
            ],
            cwd=tmp_path,
            env=box.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    pinned = dry()
    assert f"git -C {tmp_path / 'new'} checkout {TAG}" in pinned
    assert f"({TAG})" in pinned  # the plan names the ref
    development = dry("--ref", "main")
    assert " checkout " not in development and "(main)" in development
    other = dry("--ref", "v0.1.0")
    assert "checkout v0.1.0" in other
