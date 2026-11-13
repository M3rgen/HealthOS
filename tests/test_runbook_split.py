"""The runbook keeps operations; the per-revision notes live in docs/MIGRATIONS.md.

Links to the old anchors must keep resolving (index anchors), and the runbook must not grow a new
revision diary.
"""

import re
from pathlib import Path

from scripts.verify_configuration import heading_anchors

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs/RUNBOOK.md").read_text(encoding="utf-8")
MIGRATIONS = (ROOT / "docs/MIGRATIONS.md").read_text(encoding="utf-8")
INDEX_ANCHOR = re.compile(r'<a id="([^"]+)"></a>')


def test_moved_anchors_still_land_on_the_runbook():
    old = INDEX_ANCHOR.findall(RUNBOOK)
    assert old, "the runbook's index of moved sections is gone"
    assert set(old) <= heading_anchors(MIGRATIONS)


def test_runbook_has_no_revision_diary():
    assert not re.findall(r"^#+ .*\d{8}_[0-9a-z]{4}", RUNBOOK, flags=re.MULTILINE)
