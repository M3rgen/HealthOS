"""Every relative link and #anchor in the technical reference must resolve."""

from pathlib import Path

from scripts.verify_configuration import (
    MARKDOWN_TARGET,
    broken_links,
    heading_anchors,
    strip_code,
)

ROOT = Path(__file__).resolve().parents[1]


def reference_pages() -> list[Path]:
    return [ROOT / "docs" / "REFERENCE.md", *sorted((ROOT / "docs" / "reference").glob("*.md"))]


def test_reference_pages_exist():
    # The split produced real topic pages; the index alone would make the check below vacuous.
    assert {"config.md", "api.md", "cli.md"} <= {page.name for page in reference_pages()}


def test_reference_links_and_anchors_resolve():
    problems: list[str] = []
    for page in reference_pages():
        problems.extend(broken_links(page, ROOT))
        text = page.read_text(encoding="utf-8")
        anchors = heading_anchors(text)
        for target in MARKDOWN_TARGET.findall(strip_code(text)):
            if target.startswith("#") and target[1:].lower() not in anchors:
                problems.append(f"{page.relative_to(ROOT).as_posix()}: missing anchor {target}")
    assert problems == []
