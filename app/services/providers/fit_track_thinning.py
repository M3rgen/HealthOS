"""Deterministic, shape-preserving thinning of stored track samples (streaming LTTB).

Largest-Triangle-Three-Buckets runs separately on the route (longitude, latitude) and on
each chart channel against time, with an equal share of the point budget; the kept rows
are the union of their picks, the first and last sample and the lap boundaries, so every
column keeps the same rows. Each curve takes two streaming passes (bucket means, then
picks): memory grows with the point budget, never with the sample count, and Douglas–
Peucker's whole-track recursion is avoided. Ties keep the earliest row.
"""

import math
from array import array
from collections.abc import Iterable, Iterator

from .fit_track_columns import TrackFormatError, column_names, values

ROUTE: tuple[str, str] = ("longitude", "latitude")
CHART_CHANNELS = ("altitude", "speed", "heart_rate", "cadence", "power", "temperature")

type Point = tuple[int, float, float]


def curves(names: Iterable[str]) -> list[tuple[str, str]]:
    """The (x, y) channel pairs whose shape is kept; channels without data are absent."""
    present = set(names)
    found: list[tuple[str, str]] = [ROUTE] if set(ROUTE) <= present else []
    return found + [("time", name) for name in CHART_CHANNELS if name in present]


class _Buckets:
    """Pass one for one curve: per-bucket sums and the curve's first and last valid point."""

    def __init__(self, count: int, buckets: int) -> None:
        self.count, self.buckets = count, buckets
        self.sizes = array("q", [0]) * buckets
        self.sum_x, self.sum_y = array("d", [0.0]) * buckets, array("d", [0.0]) * buckets
        self.first: Point | None = None
        self.last: Point | None = None

    def bucket(self, index: int) -> int | None:
        if not self.buckets or not 0 < index < self.count - 1:
            return None
        return (index - 1) * self.buckets // (self.count - 2)

    def add(self, index: int, x: float, y: float) -> None:
        if math.isnan(x) or math.isnan(y):
            return
        self.first = self.first or (index, x, y)
        self.last = (index, x, y)
        if (bucket := self.bucket(index)) is not None:
            self.sizes[bucket] += 1
            self.sum_x[bucket] += x
            self.sum_y[bucket] += y

    def targets(self) -> list[tuple[float, float]]:
        """Each bucket's third vertex: the mean of the next non-empty bucket, else the end."""
        target = self.last[1:] if self.last else (0.0, 0.0)
        found = [target] * self.buckets
        for bucket in range(self.buckets - 1, -1, -1):
            found[bucket] = target
            if size := self.sizes[bucket]:
                target = (self.sum_x[bucket] / size, self.sum_y[bucket] / size)
        return found


class _Picker:
    """Pass two: in each bucket, the row with the largest triangle against its neighbors."""

    def __init__(self, stats: _Buckets) -> None:
        self.stats, self.targets = stats, stats.targets()
        self.anchor = stats.first[1:] if stats.first else (0.0, 0.0)
        self.current: int | None = None
        self.best: tuple[float, Point] | None = None
        self.chosen: list[int] = []

    def add(self, index: int, x: float, y: float) -> None:
        bucket = self.stats.bucket(index)
        if bucket is None or math.isnan(x) or math.isnan(y):
            return
        if bucket != self.current:
            self._commit()
            self.current = bucket
        (ax, ay), (cx, cy) = self.anchor, self.targets[bucket]
        area = abs((ax - cx) * (y - ay) - (ax - x) * (cy - ay))
        if self.best is None or area > self.best[0]:
            self.best = (area, (index, x, y))

    def _commit(self) -> None:
        if self.best is not None:
            index, x, y = self.best[1]
            self.chosen.append(index)
            self.anchor, self.best = (x, y), None

    def picks(self) -> list[int]:
        self._commit()
        ends = [point[0] for point in (self.stats.first, self.stats.last) if point]
        return [*ends, *self.chosen]


def _rows(blob: bytes, names: list[str], count: int) -> Iterator[tuple[int, tuple[float, ...]]]:
    """Rows of the named columns in lockstep; unequal lengths are a format error."""
    rows = 0
    try:
        for rows, row in enumerate(zip(*(values(blob, name) for name in names), strict=True), 1):
            yield rows - 1, row
    except ValueError:
        raise TrackFormatError() from None
    if rows != count:
        raise TrackFormatError()


def _curve_picks(blob: bytes, count: int, pairs: list[tuple[str, str]], share: int) -> set[int]:
    names = sorted({name for pair in pairs for name in pair})
    places = [(names.index(x), names.index(y)) for x, y in pairs]
    stats = [_Buckets(count, max(share - 2, 0)) for _ in pairs]
    for index, row in _rows(blob, names, count):
        for curve, (x, y) in zip(stats, places, strict=True):
            curve.add(index, row[x], row[y])
    pickers = [_Picker(curve) for curve in stats]
    for index, row in _rows(blob, names, count):
        for picker, (x, y) in zip(pickers, places, strict=True):
            picker.add(index, row[x], row[y])
    return {index for picker in pickers if share >= 2 for index in picker.picks()}


def spread(size: int, limit: int) -> list[int]:
    """At most ``limit`` positions of ``range(size)`` spread evenly, both ends included."""
    if size <= limit:
        return list(range(size))
    if limit <= 1:
        return list(range(limit))
    return [round(step * (size - 1) / (limit - 1)) for step in range(limit)]


def thin(blob: bytes, count: int, boundaries: Iterable[int], max_points: int) -> list[int] | None:
    """Sorted rows to keep (at most ``max_points``), or None when every row fits.

    The first and last rows and every lap boundary row are kept; if the boundaries alone
    exceed the budget they are spread evenly instead. ``max_points`` must be at least 2.
    """
    if count <= max_points:
        return None
    inner = sorted(set(boundaries) - {0, count - 1})
    required = {0, count - 1, *(inner[at] for at in spread(len(inner), max_points - 2))}
    budget = max_points - len(required)
    pairs = curves(column_names(blob))
    if not pairs:
        return sorted(required | set(spread(count, budget + 2)))  # Spread includes both ends.
    share = budget // len(pairs)
    return sorted(required | _curve_picks(blob, count, pairs, share))


def sample_rows(blob: bytes, moments: Iterable[float], count: int) -> dict[float, int]:
    """For each relative time, the first sample row at or after it (else the last row)."""
    if count <= 0:
        return {}
    pending = sorted({moment for moment in moments if not math.isnan(moment)})
    found: dict[float, int] = {}
    position = 0
    for index, moment in enumerate(values(blob, "time")):
        while position < len(pending) and pending[position] <= moment:
            found[pending[position]] = index
            position += 1
        if position == len(pending):
            break
    for moment in pending[position:]:
        found[moment] = count - 1
    return found
