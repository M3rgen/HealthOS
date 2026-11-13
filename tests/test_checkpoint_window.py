"""Where an incremental sync starts: its safety overlap and a clock that ran ahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.sync.checkpoints import incremental_start

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=6)


def test_first_sync_looks_back_one_window():
    assert incremental_start(None, WINDOW, NOW) == NOW - WINDOW


def test_later_sync_overlaps_by_the_window():
    assert incremental_start(NOW - timedelta(hours=1), WINDOW, NOW) == NOW - timedelta(hours=7)


def test_a_success_recorded_in_the_future_never_starts_after_now():
    # The window would still point past now: the request must not ask for tomorrow.
    assert incremental_start(NOW + timedelta(days=30), WINDOW, NOW) == NOW
