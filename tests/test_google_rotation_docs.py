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
    release = re.search(
        rf"^## \[{re.escape(project_version())}\].*?(?=^## |\Z)", changelog, re.M | re.S
    )
    assert release is not None and "consent" in release[0].lower()
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    assert "### If a root key or database dump leaks" in runbook
    upgrade = runbook.split("### Upgrade from v0.2.2", 1)[1].split("\n## ", 1)[0]
    assert '"Connect Google"' in upgrade and "consent" in upgrade.lower()
