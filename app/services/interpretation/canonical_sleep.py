"""Shared sleep estimate with independent evidence for timed stages and supplied totals."""

from collections.abc import Sequence
from typing import Any

from app.models.health import SleepSession

from .sleep_estimate import sleep_estimate
from .sleep_timeline import sleep_timeline


def sleep_evidence(sessions: Sequence[tuple[SleepSession, dict[str, Any] | None]]):
    return sleep_estimate(sessions), sleep_timeline(sessions)
