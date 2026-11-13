"""The list notice and the server cap must agree."""

import re
from pathlib import Path

from app.services.readers.dashboard_manual import MEASUREMENT_LIST_LIMIT


def test_measurement_list_limit_matches_frontend():
    source = Path("frontend/src/measurement-list.js").read_text()
    match = re.search(r"MEASUREMENT_LIMIT = (\d+)", source)
    assert match is not None
    assert int(match[1]) == MEASUREMENT_LIST_LIMIT
