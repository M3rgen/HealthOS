"""Shared sleep estimate with independent evidence for timed stages and supplied totals."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.models.health import SleepSession

from .sleep_estimate import SleepAccumulator, sleep_estimate
from .sleep_timeline import SleepTimelineAccumulator, sleep_timeline


@dataclass
class SleepEvidenceAccumulator:
    """Accumulate complete canonical evidence as each raw document is read."""

    estimate: SleepAccumulator = field(default_factory=SleepAccumulator)
    timeline: SleepTimelineAccumulator = field(default_factory=SleepTimelineAccumulator)

    def add(self, row: SleepSession, payload: dict[str, Any]) -> None:
        self.estimate.add(row, payload)
        self.timeline.add(row, payload, tie_breaker=row.google_data_point_id)

    def result(self):
        return self.estimate.result(), self.timeline.result()


def sleep_evidence(sessions: Sequence[tuple[SleepSession, dict[str, Any] | None]]):
    return sleep_estimate(sessions), sleep_timeline(sessions)
