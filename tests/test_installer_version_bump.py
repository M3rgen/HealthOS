"""`make bump-interim` / `bump-target` carry the installer extension's version along.

The installers clone their own release tag, and their documents pin install URLs to it
(tests/test_install_docs.py). A release bump therefore moves the two version markers and every
`v<version>` tag in those files with the five release files; a published-release link stays.
"""

import re
import shutil
import tomllib

import pytest
from test_bump_version import prepare_copy, run_bump, snapshot

from scripts import bump_version as bump

RAW = re.compile(r"raw\.githubusercontent\.com/M3rgen/HealthOS/([^/\s]+)/")


def installer_copy(tmp_path):
    current = tomllib.loads((bump.ROOT / "pyproject.toml").read_text())["project"]["version"]
    root = prepare_copy(tmp_path, current, status="2026-11-12")
    for name in bump.INSTALLER_FILES:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bump.ROOT / name, destination)
    return root, current


def marker(root, name):
    pattern = bump.INSTALLER_MARKERS[name] + r'([^"]*)"$'
    return re.search(pattern, (root / name).read_text(encoding="utf-8"), re.MULTILINE)[1]


@pytest.mark.parametrize("args", [["interim"], ["target", "9.0.0"]])
def test_a_bump_moves_the_installer_version_and_its_tags(tmp_path, args):
    root, current = installer_copy(tmp_path)
    before = snapshot(root)
    published = {
        name: [line for line in before[name].decode().splitlines() if "releases/tag/" in line]
        for name in bump.INSTALLER_FILES
    }
    result = run_bump(root, *args)
    assert result.returncode == 0, result.stderr
    new = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert new != current
    assert "installer extension" in result.stdout
    after = snapshot(root)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {*bump.FILES, "install.sh", "install.ps1"} | {
        name for name in bump.INSTALLER_FILES if f"v{current}".encode() in before[name]
    }
    for name in bump.INSTALLER_MARKERS:
        assert marker(root, name) == new
    old_tag = re.compile(rf"(?<![\w.])v{re.escape(current)}(?![\w]|\.[0-9])")
    for name in bump.INSTALLER_FILES:
        text = after[name].decode()
        kept = [line for line in text.splitlines() if "releases/tag/" in line]
        assert kept == published[name], name  # a published release link is never rewritten
        for line in text.splitlines():
            if "releases/tag/" not in line:
                assert not old_tag.search(line), f"{name}: {line}"
            for ref in RAW.findall(line):
                assert ref in ("main", f"v{new}"), f"{name}: {line}"


def test_a_stale_installer_marker_stops_the_bump_before_any_write(tmp_path):
    root, current = installer_copy(tmp_path)
    script = root / "install.sh"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            f'HEALTHOS_INSTALLER_VERSION="{current}"', 'HEALTHOS_INSTALLER_VERSION="0.0.1"'
        ),
        encoding="utf-8",
    )
    before = snapshot(root)
    result = run_bump(root, "interim")
    assert result.returncode == 2
    assert "install.sh installer version must match the project version" in result.stderr
    assert snapshot(root) == before


def test_a_tree_without_the_installers_changes_only_the_release_files(tmp_path):
    current = tomllib.loads((bump.ROOT / "pyproject.toml").read_text())["project"]["version"]
    root = prepare_copy(tmp_path, current, status="2026-11-12")
    before = snapshot(root)
    result = run_bump(root, "interim")
    assert result.returncode == 0, result.stderr
    assert "installer extension" not in result.stdout
    after = snapshot(root)
    assert {name for name in before if before[name] != after[name]} == set(bump.FILES)
