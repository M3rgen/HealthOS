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
    SLEEP_NUMERIC_ERROR,  # invalid evidence, including negatives, invalidates the day
    "from_normalized_columns",  # no readable raw payload: duration less awake columns
    "duration_from_payload",  # period from the payload's top-level duration (no schema)
    "awake_unknown",  # no summary or stages: the whole period, an upper bound
    "overlap_estimated",  # overlapping sessions: the least asleep fraction of each overlap
)


@dataclass(frozen=True)
class SleepEstimate:
    seconds: float | None
    assumptions: list[str] = field(default_factory=list)


def _normalized_seconds(value: object) -> float | None:
    """Validate each legacy source duration before subtraction can hide its sign."""
    seconds = finite_number(value)
    return seconds if seconds is not None and seconds >= 0 else None


@dataclass
class SleepAccumulator:
    """Consume evidence immediately; retain only boundary events and assumption codes.

    O(n) small events support the O(n log n) overlap sweep. Raw payloads and session
    objects are never retained, so their size only affects the caller's current batch.
    """

    events: dict[datetime, list[tuple[int, float, datetime]]] = field(default_factory=dict)
    found: set[str] = field(default_factory=set)

    def add(self, row: SleepSession, payload: dict | None) -> None:
        """Apply the same raw/legacy evidence rules for one session, then release it."""
        start, end = as_utc(row.start_time), as_utc(row.end_time)
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            self.found.add("invalid_sessions_skipped")
            return
        if payload is not None:
            measured = sleep_asleep_seconds(payload, start, end)
            self.found.update(measured.assumptions)
            asleep = finite_number(
                measured.period if measured.seconds is None else measured.seconds
            )
        else:
            self.found.add("from_normalized_columns")
            if row.awake_seconds is None:
                self.found.add("awake_unknown")
            duration = (
                _normalized_seconds(row.duration_seconds)
                if row.duration_seconds is not None
                else seconds
            )
            awake = _normalized_seconds(row.awake_seconds) if row.awake_seconds is not None else 0.0
            asleep = (
                finite_difference(duration, awake)
                if duration is not None and awake is not None
                else None
            )
        if asleep is None:
            self.found.add(SLEEP_NUMERIC_ERROR)
            return
        fraction = finite_scale(max(0, min(seconds, asleep)), 1.0, seconds)
        if fraction is None:
            self.found.add(SLEEP_NUMERIC_ERROR)
            return
        self.events.setdefault(start, []).append((1, fraction, end))
        self.events.setdefault(end, []).append((-1, fraction, end))

    def result(self) -> SleepEstimate:
        """Resolve half-open overlaps without rescanning active sessions."""
        events, found = self.events, self.found.copy()
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
            total if events and SLEEP_NUMERIC_ERROR not in found else None,
            [code for code in SLEEP_ASSUMPTIONS if code in found],
        )


def sleep_estimate(sessions: Sequence[tuple[SleepSession, dict | None]]) -> SleepEstimate:
    """One day's asleep total from (session, sleep payload or None) pairs.

    Raw evidence determines asleep time; without it, legacy normalized columns do.
    Each overlap takes the least asleep fraction, counted once. Keep every original
    boundary to preserve floating-point accumulation and ordered assumption codes.
    """
    accumulator = SleepAccumulator()
    for row, payload in sessions:
        accumulator.add(row, payload)
    return accumulator.result()


def sleep_total(sessions: list[SleepSession]) -> float | None:
    """Asleep total from normalized columns alone; see `sleep_estimate`."""
    return sleep_estimate([(row, None) for row in sessions]).seconds
