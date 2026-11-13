"""Current step/sleep intervals and their civil-window allocation, shared by readers.

Candidate eligibility and source resolution belong to callers. Canonical evidence can
retain a missing step count on its current raw bounds; the dashboard requires a value.
Keep whole-interval and hourly arithmetic separate to preserve their float rounding.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.models.health import SleepSession

from .current_sleep import current_sleep, sleep_wake_window
from .current_steps import current_steps


def interval_window(kind: str, left: Any, right: Any, start: datetime, stop: datetime):
    """The same half-open predicates for SQL candidates and validated payload dates."""
    if kind == "sleep":
        return sleep_wake_window(right, start, stop)
    return left < stop, right > start


@dataclass(frozen=True)
class StepWindow:
    left: datetime
    right: datetime
    count: float | None
    start: datetime
    stop: datetime

    @property
    def term(self) -> tuple[float | None, float, float]:
        return (
            self.count,
            (self.stop - self.start).total_seconds(),
            (self.right - self.left).total_seconds(),
        )

    def hourly_terms(self, zone: ZoneInfo):
        """Split in UTC at civil-hour edges, including midnight and repeated DST hours."""
        cursor = self.start
        while cursor < self.stop:
            local = cursor.astimezone(zone)
            edge = min(
                cursor
                + timedelta(
                    seconds=3600 - local.minute * 60 - local.second - local.microsecond / 1e6
                ),
                self.stop,
            )
            yield local, (self.count, (edge - cursor).total_seconds(), self.term[2])
            cursor = edge


def step_window(
    record: dict[str, Any],
    start: datetime,
    stop: datetime,
    *,
    missing_bounds: tuple[datetime | None, datetime | None] | None = None,
) -> StepWindow | None:
    measurement = current_steps(record)
    if measurement is None:
        if missing_bounds is None:
            return None
        left, right = missing_bounds
        if left is None or right is None:
            return None
        count = None
    else:
        left, right, count = measurement
    if not all(interval_window("steps", left, right, start, stop)):
        return None
    return StepWindow(left, right, count, max(left, start), min(right, stop))


def sleep_window(
    record: dict[str, Any], start: datetime, stop: datetime, *, identifier: str | None = None
):
    measurement = current_sleep(record)
    if measurement is None:
        return None
    left, right, payload = measurement
    if not all(interval_window("sleep", left, right, start, stop)):
        return None
    # A detached span never changes retained archive columns, even in the identity map.
    span = SleepSession(google_data_point_id=identifier, start_time=left, end_time=right)
    return span, payload
