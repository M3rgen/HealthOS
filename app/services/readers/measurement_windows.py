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
        """Stream UTC durations within one civil date/hour and constant UTC offset.

        Repeated hours keep the same label; callers merge their contributions into
        the existing 24 buckets. No contribution is rounded or retained here.
        """
        cursor = self.start
        while cursor < self.stop:
            local = cursor.astimezone(zone)
            edge = _hour_edge(cursor, self.stop, local, zone)
            yield local, (self.count, (edge - cursor).total_seconds(), self.term[2])
            cursor = edge


def _hour_edge(cursor: datetime, stop: datetime, local: datetime, zone: ZoneInfo) -> datetime:
    """The next wall-hour boundary or earlier offset transition, on the UTC axis.

    ZoneInfo exposes offsets rather than a public transition table. Probe no
    farther than the next nominal hour (at most one UTC hour). IANA transitions
    are separated by more than an hour, so an offset difference brackets exactly
    one transition, even when the local hour label repeats across it.

    Bisect that bracket in integer timedelta arithmetic: at most 32 probes for
    an hour, with no second/microsecond scan and no floating-point edge rounding.
    The lower endpoint retains the old offset; the upper endpoint has the new
    one. Returning the upper endpoint guarantees progress even one µs away.
    """
    remaining = timedelta(hours=1) - timedelta(
        minutes=local.minute, seconds=local.second, microseconds=local.microsecond
    )
    # Compare before adding so a clipped window at datetime.max cannot overflow.
    edge = cursor + min(remaining, stop - cursor)
    offset = local.utcoffset()
    if edge.astimezone(zone).utcoffset() == offset:
        return edge
    lower = cursor
    resolution = timedelta(microseconds=1)
    while edge - lower > resolution:
        middle = lower + (edge - lower) // 2
        if middle.astimezone(zone).utcoffset() == offset:
            lower = middle
        else:
            edge = middle
    return edge


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
