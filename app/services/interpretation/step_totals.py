"""Per-platform step arithmetic and a chronological sweep of raw-free interval tails."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime
from heapq import heappop, heappush
from itertools import count
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from app.services.shared.finite_math import FiniteScaledSum

if TYPE_CHECKING:
    from app.services.readers.measurement_windows import StepWindow

Term = tuple[float | None, float, float]
Terms = Iterator[tuple[datetime, Term]]


class StepTotals:
    """Keep the existing hourly proportions, compensated sum and final daily rounding."""

    def __init__(self, bucket_day: date | None = None):
        self.days: dict[date, dict[str, FiniteScaledSum]] = {}
        self.buckets: dict[str, dict[int, FiniteScaledSum]] = defaultdict(
            lambda: defaultdict(FiniteScaledSum)
        )
        self.bucket_day = bucket_day

    def add(self, source: str, local: datetime, term: Term) -> None:
        day = local.date()
        totals = self.days.setdefault(day, {})
        if source not in totals:
            totals[source] = FiniteScaledSum()
        value, multiplier, divisor = term
        value = cast(float, value)  # step_window without missing_bounds requires a count
        totals[source].add(value, multiplier, divisor)
        if day == self.bucket_day:
            self.buckets[source][local.hour].add(value, multiplier, divisor)

    def finish(self, before: int) -> Iterator[tuple[date, str, int | None]]:
        for day in sorted(self.days):
            if day.toordinal() >= before:
                break
            totals = self.days.pop(day)
            for source in tuple(totals):
                value = totals.pop(source).result()
                yield day, source, round(value) if value is not None else None


class StepSweep:
    """Merge interval tails without retaining JSON or materializing future days.

    Input starts must be in UTC order. Only unfinished intervals keep an iterator
    (numeric bounds/count, never a raw document); each has one pending hour. Dense
    single-hour records finish immediately. Pending state is proportional to the
    concurrently unfinished intervals, rather than archive length or duration.

    UTC hour order need not imply civil date order during rollbacks. Since offsets
    are within +/-24 hours, at UTC day D only local days D-1 and later remain open.
    This also covers transitions that repeat midnight or skip an entire date.
    """

    def __init__(self, zone: ZoneInfo):
        self.zone = zone
        self.totals = StepTotals()
        self.pending: list[tuple[datetime, int, str, datetime, Term, Terms]] = []
        self.serial = count()

    def _schedule(self, source: str, terms: Terms) -> None:
        item = next(terms, None)
        if item is not None:
            local, term = item
            heappush(
                self.pending,
                (local.astimezone(UTC), next(self.serial), source, local, term, terms),
            )

    def add(self, source: str, allocation: StepWindow) -> None:
        terms = allocation.hourly_terms(self.zone)
        local, term = next(terms)
        self.totals.add(source, local, term)
        self._schedule(source, terms)

    def advance(self, before: datetime | None) -> Iterator[tuple[date, str, int | None]]:
        while self.pending and (before is None or self.pending[0][0] < before):
            instant, _, source, local, term, terms = heappop(self.pending)
            yield from self.totals.finish(instant.date().toordinal() - 1)
            self.totals.add(source, local, term)
            self._schedule(source, terms)
        yield from self.totals.finish(
            before.date().toordinal() - 1 if before is not None else date.max.toordinal() + 1
        )
