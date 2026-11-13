"""Operator examples must keep credentials out of process arguments and history."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNSAFE_CREDENTIAL = re.compile(r"Bearer \$\{?(?:APP_UI_TOKEN|TOKEN)\b|PASSWORD '<")


def test_documented_commands_do_not_expose_credentials():
    docs = ROOT / "docs"
    pages = [ROOT / "README.md", *sorted(docs.rglob("*.md"))]
    violations = [
        f"{page.relative_to(ROOT)}:{number}"
        for page in pages
        if not page.is_relative_to(docs / "runbooks")
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if UNSAFE_CREDENTIAL.search(line)
    ]
    assert violations == [], "Credentials exposed in documentation: " + ", ".join(violations)
