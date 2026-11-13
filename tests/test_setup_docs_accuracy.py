"""Keep setup instructions aligned with the supported runtime and backup schedule."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_backup_times_include_utc():
    paths = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    missing = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in paths
        if not path.name.endswith(".tr.md")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "03:30" in line and "UTC" not in line
    ]
    assert missing == []


def test_readme_quickstart_protects_env_permissions():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = readme.split("### 1. Clone and configure\n", 1)[1].split("### 2.", 1)[0]
    assert "chmod 600 .env" in quickstart


def test_readme_creates_backup_status_before_compose():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    before_compose = readme.split("### 1. Clone and configure\n", 1)[1].split(
        "docker compose up", 1
    )[0]
    assert "make env\ninstall -d -m 755 backup-status\n" in before_compose


def test_setup_python_versions_match_project_requirement():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    bounds = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", project["project"]["requires-python"])
    assert bounds is not None, "Update the documentation check for the new Python range"
    expected = [f"3.{minor}" for minor in range(int(bounds[1]), int(bounds[2]))]
    setup = (ROOT / "docs/reference/setup.md").read_text(encoding="utf-8")
    versions = re.search(r"\bPython (3\.\d+(?: or 3\.\d+)*)(?= and PostgreSQL)", setup)
    assert versions is not None, "Document explicit supported Python versions"
    assert versions[1].split(" or ") == expected
