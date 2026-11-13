"""The layout floors in tests/browser/viewport-floors.json are a ratchet.

A floor stated for a viewport (visible card, chart and history height, full history rows) must be
what the browser matrix asserts, and it must not be lowered to fit a measured result. The minimums
below are deliberately repeated here: lowering a floor means changing this test, in the open, and
that decision belongs to the maintainer (CONTRIBUTING.md, Interface requirements).
"""

import json
import re
from pathlib import Path

BROWSER_TESTS = Path(__file__).resolve().parent / "browser"
STYLES = Path(__file__).resolve().parent.parent / "frontend" / "src"
FLOORS = json.loads((BROWSER_TESTS / "viewport-floors.json").read_text(encoding="utf-8"))
MATRIX = (BROWSER_TESTS / "viewport-matrix.cjs").read_text(encoding="utf-8")

# (width, height): (card, chart, history, history rows). Only ever raise these.
MINIMUMS = {
    (320, 256): (70, 0, 44, 0),
    (320, 320): (120, 0, 44, 0),
    (375, 667): (200, 100, 90, 2),
    (390, 844): (260, 150, 90, 3),
    (844, 390): (180, 60, 150, 3),
    (1024, 600): (120, 40, 90, 3),
    (1280, 256): (100, 0, 100, 0),
    (1280, 420): (180, 60, 150, 3),
    (1280, 500): (120, 40, 90, 2),
    (1366, 649): (120, 40, 90, 3),
    (1366, 650): (120, 40, 90, 3),
    (1366, 768): (120, 40, 90, 3),
    (1440, 900): (140, 40, 120, 3),
    (1920, 1080): (150, 40, 120, 7),
}

# A chart under this height at a laptop or desktop size is not a chart any more.
LAPTOP_CHART_FLOOR = 40


def rows():
    return {(row[0], row[1]): tuple(row[2:]) for row in FLOORS["sizes"]}


def test_matrix_reads_the_stated_floors():
    assert "require('./viewport-floors.json')" in MATRIX
    assert "const SIZES = FLOORS.sizes;" in MATRIX


def test_every_stated_viewport_keeps_its_minimums():
    stated = rows()
    missing = sorted(set(MINIMUMS) - set(stated))
    assert not missing, f"viewport removed from the floor table: {missing}"
    lowered = {
        size: (stated[size], minimum)
        for size, minimum in MINIMUMS.items()
        if any(now < floor for now, floor in zip(stated[size], minimum, strict=True))
    }
    assert not lowered, f"floors lowered (stated, minimum): {lowered}"


def test_short_laptops_keep_a_readable_chart():
    too_small = {
        size: values[1]
        for size, values in rows().items()
        if size[0] > 900 and size[1] >= 481 and values[1] < LAPTOP_CHART_FLOOR
    }
    assert not too_small, f"chart floor under {LAPTOP_CHART_FLOOR} px at laptop sizes: {too_small}"


def test_breakpoints_follow_the_browser_font():
    """A px breakpoint ignores the font preference, and the layout then overlaps at 200 %."""
    pixel_query = re.compile(r"\((?:max|min)-(?:width|height):\s*(\d+(?:\.\d+)?)px\)")
    # 420 px is on purpose physical: a screen that short cannot spare a second row at any font.
    allowed = {"420"}
    found = [
        f"{path.name}: {line.strip()}"
        for path in STYLES.rglob("*.css")
        for line in path.read_text(encoding="utf-8").splitlines()
        if "@media" in line and set(pixel_query.findall(line)) - allowed
    ]
    assert not found, f"use em in media queries: {found}"
