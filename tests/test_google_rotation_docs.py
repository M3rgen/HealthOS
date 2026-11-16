"""Google key-rotation instructions must match mandatory reauthorization."""

import re
from pathlib import Path

import pytest
from test_version_agreement import project_version

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("obsolete", ["MultiFernet", "No Google consent is needed"])
def test_google_docs_do_not_promise_retired_token_reads(obsolete):
    pages = [ROOT / "CHANGELOG.md", *(ROOT / "docs").rglob("*.md")]
    assert [str(path.relative_to(ROOT)) for path in pages if obsolete in path.read_text()] == []


def test_google_upgrade_and_compromise_instructions():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    # The upgrade note belongs to the release that introduced reauthorization (0.2.2.1); later
    # sections need not repeat it, and an interim section may be empty.
    assert project_version() in changelog
    releases = re.findall(r"^## \[[^\]]+\].*?(?=^## |\Z)", changelog, re.M | re.S)
    assert any('"Connect Google"' in text and "consent" in text.lower() for text in releases)
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    assert "### If a root key or database dump leaks" in runbook
    upgrade = runbook.split("### Upgrade from v0.2.2", 1)[1].split("\n## ", 1)[0]
    assert '"Connect Google"' in upgrade and "consent" in upgrade.lower()
