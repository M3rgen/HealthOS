"""Every declared version agrees with pyproject.toml, the single source of the code's version."""

import json
import re
import tomllib
from pathlib import Path

import pytest

from app.core.version import DISTRIBUTION, app_version
from scripts.bump_version import npm_version

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


@pytest.mark.parametrize(
    "version, expected",
    [("0.2.3", "0.2.3"), ("0.2.2.1", "0.2.2+1"), ("0.2.2.12", "0.2.2+12")],
)
def test_npm_version_mapping(version, expected):
    assert npm_version(version) == expected


@pytest.mark.parametrize("version", ["0.2", "0.2.2.1.1", "0.2.2.0"])
def test_npm_version_rejects_invalid_versions(version):
    with pytest.raises(ValueError):
        npm_version(version)


def test_uv_lock_declares_the_project_version():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    entry = next(item for item in lock["package"] if item["name"] == DISTRIBUTION)
    assert entry["version"] == project_version()


def test_frontend_manifests_declare_the_project_version():
    package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == npm_version(project_version())
    assert lock["version"] == npm_version(project_version())
    assert lock["packages"][""]["version"] == npm_version(project_version())


def test_the_application_reports_the_installed_project_version():
    from app.main import create_app

    assert app_version() == project_version(), "stale install: run `uv sync --frozen --extra dev`"
    assert create_app().version == project_version()


def test_declared_and_published_versions_are_in_the_changelog():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    first = re.search(r"^## (.+)$", changelog, re.MULTILINE)
    assert first is not None
    assert first[1].startswith(f"[{project_version()}] - ")
    published = re.search(r"\*\*Latest published release: \[v(?P<version>[\d.]+)\]", readme)
    assert published is not None
    assert f"## [{published['version']}]" in changelog
