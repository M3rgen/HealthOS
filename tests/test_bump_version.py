"""Release bumps validate every input before changing the five-file release set."""

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import test_version_agreement as agreement

from scripts import bump_version as bump

ROOT = Path(__file__).resolve().parents[1]


def prepare_copy(root, version, status="2026-11-12"):
    current = agreement.project_version()
    for name in (*bump.FILES, "README.md", "Makefile", "scripts/bump_version.py"):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    project = root / "pyproject.toml"
    project.write_text(
        project.read_text().replace(f'version = "{current}"', f'version = "{version}"', 1)
    )
    lock = root / "uv.lock"
    lock.write_text(
        lock.read_text().replace(
            f'name = "healthos"\nversion = "{current}"', f'name = "healthos"\nversion = "{version}"'
        )
    )
    for name in ("frontend/package.json", "frontend/package-lock.json"):
        path = root / name
        data = json.loads(path.read_text())
        data["version"] = bump.npm_version(version)
        if name.endswith("package-lock.json"):
            data["packages"][""]["version"] = bump.npm_version(version)
        path.write_text(json.dumps(data, indent=2) + "\n")
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        re.sub(
            r"^## .+$",
            f"## [{version}] - {status}",
            changelog.read_text(),
            count=1,
            flags=re.MULTILINE,
        )
    )
    # The copy's README names the copy's own version, whatever the repository last published.
    readme = root / "README.md"
    readme.write_text(
        re.sub(
            r"(\*\*Latest published release: \[v)[\d.]+(\])",
            rf"\g<1>{version}\g<2>",
            readme.read_text(),
            count=1,
        )
    )
    return root


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def run_bump(root, *args):
    return subprocess.run(
        [sys.executable, str(root / "scripts/bump_version.py"), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize(
    "current, args, expected, npm",
    [
        ("0.2.2.1", ["interim"], "0.2.2.2", "0.2.2+2"),
        ("0.2.3", ["interim"], "0.2.3.1", "0.2.3+1"),
        ("0.2.2.5", ["target", "0.2.3"], "0.2.3", "0.2.3"),
        ("0.2.2.9", ["interim"], "0.2.2.10", "0.2.2+10"),
        ("0.2.9.5", ["target", "0.2.10"], "0.2.10", "0.2.10"),
    ],
)
def test_bump_updates_only_release_fields(tmp_path, monkeypatch, current, args, expected, npm):
    root = prepare_copy(tmp_path, current)
    before = snapshot(root)
    result = run_bump(root, *args)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{current} -> {expected} (npm {npm}); updated five release files\n"
    after = snapshot(root)
    assert before.keys() == after.keys()
    assert {name for name in before if before[name] != after[name]} == set(bump.FILES)

    assert tomllib.loads(after["pyproject.toml"].decode())["project"]["version"] == expected
    assert after["pyproject.toml"] == before["pyproject.toml"].replace(
        f'version = "{current}"'.encode(), f'version = "{expected}"'.encode(), 1
    )
    assert after["uv.lock"] == before["uv.lock"].replace(
        f'name = "healthos"\nversion = "{current}"'.encode(),
        f'name = "healthos"\nversion = "{expected}"'.encode(),
        1,
    )
    for name in ("frontend/package.json", "frontend/package-lock.json"):
        data = json.loads(before[name])
        data["version"] = npm
        if name.endswith("package-lock.json"):
            data["packages"][""]["version"] = npm
        assert after[name] == (json.dumps(data, indent=2) + "\n").encode()
    old_heading = f"## [{current}] - 2026-11-12".encode()
    assert after["CHANGELOG.md"] == before["CHANGELOG.md"].replace(
        old_heading,
        f"## [{expected}] - Unreleased\n\n- (no changes yet)\n\n".encode() + old_heading,
        1,
    )
    monkeypatch.setattr(agreement, "ROOT", root)
    agreement.test_uv_lock_declares_the_project_version()
    agreement.test_frontend_manifests_declare_the_project_version()
    agreement.test_declared_and_published_versions_are_in_the_changelog()


@pytest.mark.parametrize("args", [["interim"], ["target", "0.2.3"]])
def test_unreleased_section_refuses_without_writes(tmp_path, args):
    root = prepare_copy(tmp_path, "0.2.2.1", "Unreleased")
    before = snapshot(root)
    result = run_bump(root, *args)
    assert result.returncode == 2
    assert result.stderr == "the current section is unreleased; date it when you tag 0.2.2.1\n"
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "target",
    ["0.2.2", "0.2.1", "0.2.2.6", "0.2", "v0.2.3", "0.2.3+1", "0.2.3rc1", "00.2.3"],
)
def test_invalid_or_nonincreasing_target_never_writes(tmp_path, target):
    root = prepare_copy(tmp_path, "0.2.2.5")
    before = snapshot(root)
    result = run_bump(root, "target", target)
    assert result.returncode == 2
    assert snapshot(root) == before


def test_equal_target_is_rejected(tmp_path):
    root = prepare_copy(tmp_path, "0.2.3")
    before = snapshot(root)
    assert run_bump(root, "target", "0.2.3").returncode == 2
    assert snapshot(root) == before


@pytest.mark.parametrize("name", bump.FILES)
@pytest.mark.parametrize("defect", ["missing-file", "missing-line", "mismatch"])
def test_incomplete_or_inconsistent_release_set_never_writes(tmp_path, name, defect):
    root = prepare_copy(tmp_path, "0.2.2.7")
    path = root / name
    if defect == "missing-file":
        path.unlink()
    else:
        text = path.read_text()
        if name == "CHANGELOG.md":
            text = text.replace(
                "## [0.2.2.7] - 2026-11-12",
                "" if defect == "missing-line" else "## [9.9.9] - 2026-11-12",
            )
        else:
            old = '"version": "0.2.2+7"' if name.endswith(".json") else 'version = "0.2.2.7"'
            text = text.replace(
                old,
                old.replace("version", "missing")
                if defect == "missing-line"
                else old.replace("0.2.2", "9.9.9"),
                1,
            )
        path.write_text(text)
    before = snapshot(root)
    assert run_bump(root, "interim").returncode == 2
    assert snapshot(root) == before


@pytest.mark.parametrize("replacement", ["", '      "version": "9.9.9",\n'])
def test_missing_or_stale_nested_package_lock_version_never_writes(tmp_path, replacement):
    root = prepare_copy(tmp_path, "0.2.2.1")
    path = root / "frontend/package-lock.json"
    path.write_text(path.read_text().replace('      "version": "0.2.2+1",\n', replacement, 1))
    before = snapshot(root)
    assert run_bump(root, "interim").returncode == 2
    assert snapshot(root) == before


def test_second_bump_requires_dating_the_new_section(tmp_path):
    root = prepare_copy(tmp_path, "0.2.2.1")
    assert run_bump(root, "interim").returncode == 0
    before = snapshot(root)
    result = run_bump(root, "interim")
    assert result.returncode == 2
    assert "date it when you tag 0.2.2.2" in result.stderr
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "target, expected",
    [("bump-interim", "0.2.2.2 (npm 0.2.2+2)"), ("bump-target", "0.2.3 (npm 0.2.3)")],
)
def test_make_targets_on_a_copy(tmp_path, target, expected):
    root = prepare_copy(tmp_path, "0.2.2.1")
    result = subprocess.run(
        ["make", target, "VERSION=0.2.3"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"0.2.2.1 -> {expected}; updated five release files" in result.stdout
