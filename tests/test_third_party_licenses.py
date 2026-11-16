"""THIRD_PARTY_LICENSES.md stays in step with frontend/package-lock.json."""

import copy
import json

import pytest

from scripts import third_party_licenses as notices

ROOT = notices.ROOT


def test_notice_file_matches_the_lockfile():
    problems = notices.lock_mismatches(notices.read_lock(), notices.OUTPUT.read_text("utf-8"))
    assert not problems, (
        "THIRD_PARTY_LICENSES.md is stale; run `npm ci` in frontend/ and "
        f"`python3 scripts/third_party_licenses.py`: {problems}"
    )


def test_every_runtime_dependency_is_covered():
    manifest = json.loads((ROOT / "frontend/package.json").read_text("utf-8"))
    names = {package.name for package in notices.runtime_packages(notices.read_lock())}
    assert set(manifest["dependencies"]) <= names
    assert not set(manifest["devDependencies"]) & names, "build tools are not shipped"


def test_a_version_change_without_regenerating_fails():
    lock = notices.read_lock()
    text = notices.OUTPUT.read_text("utf-8")
    bumped = copy.deepcopy(lock)
    bumped["packages"]["node_modules/marked"]["version"] = "999.0.0"
    problems = notices.lock_mismatches(bumped, text)
    assert "missing from the table: marked 999.0.0 (MIT)" in problems
    assert any(problem.startswith("stale licence section: marked ") for problem in problems)


def test_every_entry_carries_a_licence_text():
    text = notices.OUTPUT.read_text("utf-8")
    for package in notices.runtime_packages(notices.read_lock()):
        heading = f"\n## {package.name} {package.version}\n"
        assert heading in text
        section = text.split(heading, 1)[1]
        section = notices.FENCE.sub("<text>", section).split("\n## ", 1)[0]
        assert "\n<text>" in section, package.name


@pytest.mark.skipif(
    not (ROOT / "frontend/node_modules").is_dir(), reason="needs `npm ci` in frontend/"
)
def test_notice_file_is_exactly_what_the_installed_packages_give():
    assert notices.render() == notices.OUTPUT.read_text("utf-8")


def _package(root, name, version, files, manifest=None):
    directory = root / "node_modules" / name
    directory.mkdir(parents=True)
    (directory / "package.json").write_text(json.dumps(manifest or {"name": name}))
    for file, content in files.items():
        (directory / file).write_text(content)


def test_render_reads_licence_files_readme_sections_and_vendored_copies(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    lock = {
        "packages": {
            "": {"name": "fixture"},
            "node_modules/plain": {"version": "1.0.0", "license": "MIT"},
            "node_modules/readme-only": {"version": "2.0.0", "license": "MIT"},
            "node_modules/vendored": {"version": "3.0.0", "license": "BSD-3-Clause"},
            "node_modules/@types/thing": {"version": "1.0.0", "license": "MIT"},
            "node_modules/tool": {"version": "4.0.0", "license": "MIT", "dev": True},
        }
    }
    frontend.mkdir()
    (frontend / "package-lock.json").write_text(json.dumps(lock))
    _package(frontend, "plain", "1.0.0", {"LICENSE": "Copyright (c) 2020 Plain Author\n\nTerms."})
    _package(
        frontend,
        "readme-only",
        "2.0.0",
        {"README.md": "# Readme\n\n## License\n\nCopyright (c) 2011 Someone\n\nMIT terms.\n"},
    )
    _package(frontend, "vendored", "3.0.0", {})
    (frontend / "licenses").mkdir()
    (frontend / "licenses/vendored.LICENSE").write_text("Copyright 2021 Vendor\n\nBSD terms.\n")
    monkeypatch.setitem(
        notices.VENDORED, "vendored", ("frontend/licenses/vendored.LICENSE", "https://example.org")
    )
    text = notices.render(frontend)
    assert "## plain 1.0.0" in text and "Copyright (c) 2020 Plain Author" in text
    assert "## readme-only 2.0.0" in text and "Text (README licence section)" in text
    assert "- Copyright: Copyright 2021 Vendor" in text
    assert "@types/thing" not in text and "tool" not in text
    assert notices.lock_mismatches(lock, text) == []


def test_render_refuses_a_package_without_any_licence_text(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    lock = {"packages": {"node_modules/bare": {"version": "1.0.0", "license": "MIT"}}}
    (frontend / "package-lock.json").write_text(json.dumps(lock))
    _package(frontend, "bare", "1.0.0", {"README.md": "# Bare\n"})
    with pytest.raises(SystemExit, match="no licence text for bare@1.0.0"):
        notices.render(frontend)
