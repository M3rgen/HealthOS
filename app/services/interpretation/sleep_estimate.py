"""Shared asleep estimate for normalized Google and Garmin sleep sessions."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from heapq import heappop, heappush
from itertools import pairwise

from app.core.timeutil import as_utc
from app.models.health import SleepSession
from app.parsers.sleep import SLEEP_NUMERIC_ERROR, sleep_asleep_seconds
from app.services.shared.finite_math import finite_difference, finite_number, finite_scale

# Machine-readable reasons a day's sleep value is not a plain measurement, in this order.
SLEEP_ASSUMPTIONS = (
    "invalid_sessions_skipped",  # sessions ending at or before their start are left out
    SLEEP_NUMERIC_ERROR,  # invalid evidence makes the day's total unavailable
    "from_normalized_columns",  # no readable raw payload: duration less awake columns
    "duration_from_payload",  # period from the payload's top-level duration (no schema)
    "awake_unknown",  # no summary or stages: the whole period, an upper bound
    "overlap_estimated",  # overlapping sessions: the least asleep fraction of each overlap
)


@dataclass(frozen=True)
class SleepEstimate:
    seconds: float | None
    assumptions: list[str] = field(default_factory=list)


def sleep_estimate(sessions: Sequence[tuple[SleepSession, dict | None]]) -> SleepEstimate:
    """One day's asleep total from (session, sleep payload or None) pairs.

    The payload gives asleep time by its shape (`sleep_asleep_seconds`); without one the
    normalized duration less awake columns do. Overlapping sessions are counted once:
    stage totals cannot locate awake intervals, so each overlap takes the least asleep
    fraction; this conservative estimate is exposed. No valid session is no value.

    Ordered boundary events and a lazy minimum heap take O(n log n) time and O(n)
    space. Keep every original boundary so floating-point accumulation is unchanged.
    """
    if not sessions:
        return SleepEstimate(None)
    found: set[str] = set()
    spans = []
    for row, payload in sessions:
        start, end = as_utc(row.start_time), as_utc(row.end_time)
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            found.add("invalid_sessions_skipped")
            continue
        if payload is not None:
            measured = sleep_asleep_seconds(payload, start, end)
            found.update(measured.assumptions)
            asleep = finite_number(
                measured.period if measured.seconds is None else measured.seconds
            )
        else:
            found.add("from_normalized_columns")
            if row.awake_seconds is None:
                found.add("awake_unknown")
            duration = (
                finite_number(row.duration_seconds) if row.duration_seconds is not None else seconds
            )
            awake = finite_number(row.awake_seconds) if row.awake_seconds is not None else 0.0
            asleep = (
                finite_difference(duration, awake)
                if duration is not None and awake is not None
                else None
            )
        if asleep is None:
            found.add(SLEEP_NUMERIC_ERROR)
            continue
        fraction = finite_scale(max(0, min(seconds, asleep)), 1.0, seconds)
        if fraction is None:
            found.add(SLEEP_NUMERIC_ERROR)
            continue
        spans.append((start, end, fraction))
    events: dict[datetime, list[tuple[int, float, datetime]]] = {}
    for start, end, fraction in spans:
        events.setdefault(start, []).append((1, fraction, end))
        events.setdefault(end, []).append((-1, fraction, end))
    # Each span enters the minimum heap once and leaves at most once. Expired
    # nonminimum entries may stay buried until needed; no active set is rescanned.
    minimums: list[tuple[float, datetime]] = []
    active = 0
    total = 0.0
    for left, right in pairwise(sorted(events)):
        # End events precede starts at equal times: intervals are half open.
        for delta, fraction, end in sorted(events[left]):
            active += delta
            if delta > 0:
                heappush(minimums, (fraction, end))
        # A minimum may have ended even when another span keeps active nonzero.
        while minimums and minimums[0][1] <= left:
            heappop(minimums)
        if active:
            fraction = minimums[0][0]
            total += (right - left).total_seconds() * fraction
            if active > 1 and fraction < 1:
                found.add("overlap_estimated")
    return SleepEstimate(
        total if spans and SLEEP_NUMERIC_ERROR not in found else None,
        [code for code in SLEEP_ASSUMPTIONS if code in found],
    )


def sleep_total(sessions: list[SleepSession]) -> float | None:
    """Asleep total from normalized columns alone; see `sleep_estimate`."""
    return sleep_estimate([(row, None) for row in sessions]).seconds
