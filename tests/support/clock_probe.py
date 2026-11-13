"""Proof that the clock harness works; run by ``clock_matrix.sh --preflight``, never collected.

The name keeps it out of the normal suite. A probe that always passes would be worthless, so it
reads the same environment the harness sets and checks what the process actually sees.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def test_the_clock_plugin_shifts_time_and_zone():
    assert "time_machine" in sys.modules, "clock_shift did not load time_machine"
    moment = datetime.fromisoformat(os.environ["HEALTHOS_TEST_CLOCK"])
    now = datetime.now(UTC)
    assert abs((now - moment).total_seconds()) < 300, f"clock not shifted: {now} vs {moment}"
    local = datetime.now().astimezone()
    expected = now.astimezone(ZoneInfo(os.environ["TZ"])).utcoffset()
    assert local.utcoffset() == expected, f"zone not applied: {local.utcoffset()} vs {expected}"
