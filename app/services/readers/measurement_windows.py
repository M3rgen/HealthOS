"""Current step/sleep intervals and their civil-window allocation, shared by readers.

Candidate eligibility and source resolution belong to callers. Canonical evidence can
retain a missing step count on its current raw bounds; the dashboard requires a value.
Keep whole-interval and hourly arithmetic separate to preserve their float rounding.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, false, func, literal_column, true

from app.models.health import SleepSession

from .current_sleep import current_sleep, sleep_wake_window
from .current_steps import current_steps


def interval_window(kind: str, left: Any, right: Any, start: datetime, stop: datetime):
    """The same half-open predicates for SQL candidates and validated payload dates."""
    if kind == "sleep":
        return sleep_wake_window(right, start, stop)
    return left < stop, right > start


def raw_interval_window(kind: str, raw: Any, start: datetime, stop: datetime):
    """``interval_window`` on a raw row's source bounds, plus the bound its index can use.

    The ``(data_type, source_start_time)`` index bounds an interval only from above, so
    every older interval of the type is fetched and dropped. Both windows require
    ``source_end_time >= start``, so the end is present and ``COALESCE(end, start) >= start``
    holds for every row they keep: the added predicate selects nothing else, and lets
    ``ix_raw_health_data_type_freshness`` start the scan at ``start``.
    """
    left, right = raw.source_start_time, raw.source_end_time
    return (*interval_window(kind, left, right, start, stop), func.coalesce(right, left) >= start)


# A step interval longer than this is "long"; the few there are have a partial index of
# their own (``ix_raw_health_data_long_steps``, migration 20261116_0041), whose predicate is
# ``step_is_long`` word for word (``LONG_STEP_SQL``).
LONG_STEP = timedelta(days=1)
LONG_STEPS_INDEX = "ix_raw_health_data_long_steps"
# Bounds within the years the application can hold (1-9999): only there are they subtracted.
_FIRST = "'0001-01-01 00:00:00+00'::timestamptz"
_PAST_LAST = "'10000-01-01 00:00:00+00'::timestamptz"
# Only step rows can be long: the type is part of the expression, so the statistics on it
# (migration 20261116_0041) count the long step rows among all rows, and every constant is
# written into the statement, so even a generic plan can use the partial index.
LONG_STEP_SQL = (
    "CASE WHEN data_type <> 'steps' THEN false "
    f"WHEN source_start_time >= {_FIRST} AND source_start_time < {_PAST_LAST} "
    f"AND source_end_time >= {_FIRST} AND source_end_time < {_PAST_LAST} "
    "THEN (source_end_time - source_start_time) > interval '1 day' ELSE true END"
)


def step_is_long(raw: Any):
    """A step row longer than ``LONG_STEP``, or not measurable: a bound missing, infinite or
    outside the years 1-9999 (``LONG_STEP_SQL``; any other type is never long).

    Only bounds in that range are subtracted, so neither this nor the index predicate can
    raise for any row the database accepts (a difference of two far-apart timestamps
    overflows ``interval``; PostgreSQL before 17 refuses infinite ones). Such rows count as
    long, which keeps them out of the closed range and in the long part.
    """
    left, right = raw.source_start_time, raw.source_end_time
    first, past_last = literal_column(_FIRST), literal_column(_PAST_LAST)
    measurable = and_(left >= first, left < past_last, right >= first, right < past_last)
    return case(
        (raw.data_type != literal_column("'steps'"), false()),
        (measurable, (right - left) > literal_column("interval '1 day'")),
        else_=true(),
    )


def raw_step_window_parts(raw: Any, start: datetime, stop: datetime):
    """``raw_interval_window("steps", ...)`` split in two disjoint parts, each bounded on
    both sides by an index, for PostgreSQL readers (a union of the two is the same rows).

    ``raw_interval_window`` bounds the ``COALESCE(end, start)`` index from below only, so an
    older window read every later step row up to today. A finite interval of at most
    ``LONG_STEP`` that meets the window ends before ``stop + LONG_STEP``: that bound closes the
    range. The others, which can start any time before, come from their own small index.
    """
    left, right = raw.source_start_time, raw.source_end_time
    window = raw_interval_window("steps", raw, start, stop)
    longer = step_is_long(raw)
    try:
        bounded = (func.coalesce(right, left) < stop + LONG_STEP,)
    except OverflowError:  # a window ending at the calendar's end: nothing later to leave out
        bounded = ()
    # The long part leaves out the COALESCE bound (implied by ``end > start`` there): with it
    # the planner, which has no statistics for an interval's length, reads the one-sided
    # freshness range instead of the long rows' own index.
    return (
        (*window, ~longer, *bounded),
        (*interval_window("steps", left, right, start, stop), longer),
    )


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
