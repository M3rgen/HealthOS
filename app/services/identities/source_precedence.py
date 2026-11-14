"""Source precedence: which source is shown when several report the same data.

The maintainer's decision (7 November 2026): for **sport activities** Garmin comes first,
for **every other health metric** the Fitbit comes first. Every source's records stay in the
archive; precedence only *selects* what a reader shows first and never sums or averages
across sources (no double counting). The policy is configuration, not code:
``SOURCE_PRECEDENCE_ACTIVITY`` and ``SOURCE_PRECEDENCE_HEALTH`` list source codes most
preferred first; reversing one is one line (app/core/config.py, .env.example).

Vocabulary:

* a **source code** names the device platform a record came from, whatever the provider that
  delivered it: ``FITBIT``, ``HEALTH_KIT``, ``HEALTH_CONNECT``, ``GOOGLE_FIT`` (Google Health's
  ``dataSource.platform``), ``GARMIN`` (the Garmin Connect Personal API), ``GOOGLE`` (a Google
  record whose platform is unknown or mixed), ``FIT`` (other file manufacturers, always
  after device sources), ``MANUAL`` (entered by the owner) and ``DEMO``
  (the synthetic sample data, listed in no policy; it is only loaded into an empty archive);
* a **category** is ``activity`` or ``health``; ``CATEGORIES`` assigns every metric a reader
  resolves to one of them, and ``sample_category`` places a sample inside a
  selected workout window in ``activity`` and outside in ``health``;
* a **conflict** is the same metric from two sources over an overlapping time: the same
  calendar day for a daily value, the same minute slice for a sample, an overlapping start/end
  for a session. ``resolve`` orders the observations of one conflict by the policy: the first
  is ``selected``, the rest are ``alternatives`` (all kept, none summed).

Manual entries are never resolved against device records: the owner typed them, they are
shown as they are (``MANUAL`` is not a device). Nothing here reads or writes the database.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.providers import DEMO, FIT, GARMIN, GOOGLE

ACTIVITY = "activity"
HEALTH = "health"
MANUAL = "MANUAL"
PRIMARY, SECONDARY, UNRANKED = "primary", "secondary", "unranked"

# Metric (as readers name it) -> category. Workouts and the metrics measured inside a workout
# window are activity; everything else is health. Minute-level heart rate, calories and
# distance are health outside a selected workout window (``sample_category``).
CATEGORIES: dict[str, str] = {
    "workouts": ACTIVITY,
    "exercise": ACTIVITY,
    "activity_heart_rate": ACTIVITY,
    "activity_calories": ACTIVITY,
    "activity_distance": ACTIVITY,
    "steps": HEALTH,
    "distance": HEALTH,
    "calories": HEALTH,
    "sleep_seconds": HEALTH,
    "sleep": HEALTH,
    "resting_bpm": HEALTH,
    "resting_heart_rate": HEALTH,
    "heart_rate": HEALTH,
    "hrv_ms": HEALTH,
    "spo2": HEALTH,
    "stress": HEALTH,
    "body_battery": HEALTH,
    "respiratory_rate": HEALTH,
    "temperature": HEALTH,
    "vo2_max": HEALTH,
    "weight": HEALTH,
    "height": HEALTH,
    "body_fat": HEALTH,
}

# Source code -> what the interface shows (Turkish where the product name is translated).
LABELS: dict[str, str] = {
    "FITBIT": "Fitbit",
    GARMIN: "Garmin",
    FIT: "FIT dosyası",
    "HEALTH_KIT": "Apple Sağlık",
    "HEALTH_CONNECT": "Health Connect",
    "GOOGLE_FIT": "Google Fit",
    GOOGLE: "Google",
    MANUAL: "Manuel",
    DEMO: "Örnek veri",
}


def label(source: str | None) -> str | None:
    """The interface label of a source code (the code itself when it is not known here)."""
    if source is None:
        return None
    return LABELS.get(source, source.replace("_", " ").title())


def category(metric: str) -> str:
    """The category a metric is resolved in; an unknown metric is health."""
    return CATEGORIES.get(metric, HEALTH)


def google_source(platform: object) -> str:
    """The source code of a Google record from its ``dataSource.platform``."""
    if isinstance(platform, str) and platform.strip():
        return platform.strip().upper()
    return GOOGLE


def google_source_of(platforms: Iterable[object]) -> str:
    """The source code of a Google value made from several records: their one platform, or
    ``GOOGLE`` when they come from more than one (or none says)."""
    found = {google_source(platform) for platform in platforms}
    return next(iter(found)) if len(found) == 1 else GOOGLE


@dataclass(frozen=True)
class Policy:
    """The ordered source codes per category (most preferred first)."""

    activity: tuple[str, ...] = ("GARMIN", "FITBIT", "HEALTH_KIT")
    health: tuple[str, ...] = ("FITBIT", "GARMIN", "HEALTH_KIT")

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Policy:
        settings = settings or get_settings()
        return cls(
            tuple(settings.source_precedence_activity.split(",")),
            tuple(settings.source_precedence_health.split(",")),
        )

    def order(self, category: str) -> tuple[str, ...]:
        return self.activity if category == ACTIVITY else self.health

    def rank(self, source: str, category: str) -> int:
        """Position in the category's list; unlisted sources rank after every listed one
        (``GOOGLE``, a Google record of unknown platform, right after them, then by name).
        Other FIT manufacturers always follow every device, including unlisted sources;
        putting FIT in a configured list cannot promote it or reorder MANUAL/DEMO.
        """
        order = tuple(code for code in self.order(category) if code != FIT)
        if source == FIT:
            return len(order) + 2
        if source in order:
            return order.index(source)
        return len(order) + (0 if source == GOOGLE else 1)

    def priority(self, source: str, category: str) -> str:
        """``primary`` when the source leads the category, ``secondary`` when it is listed
        after another, ``unranked`` when the policy does not list it."""
        if source == FIT:
            return SECONDARY
        order = tuple(code for code in self.order(category) if code != FIT)
        if source not in order:
            return UNRANKED
        return PRIMARY if order[0] == source else SECONDARY

    def as_dict(self) -> dict[str, list[str]]:
        return {ACTIVITY: list(self.activity), HEALTH: list(self.health)}


@dataclass(frozen=True)
class Observation:
    """One source's value of one metric over one time (a day, a minute, a session)."""

    source: str
    value: Any
    provider: str = GOOGLE
    record_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def as_dict(self, *, with_value: bool = True) -> dict[str, Any]:
        item: dict[str, Any] = {"source": self.source, "label": label(self.source)}
        if with_value:
            item["value"] = self.value
        if self.record_id is not None:
            item["id"] = self.record_id
        return item


@dataclass(frozen=True)
class Resolution:
    """The outcome of one conflict: the shown observation and the ones kept visible."""

    selected: Observation | None
    alternatives: tuple[Observation, ...] = ()

    @property
    def value(self) -> Any:
        return self.selected.value if self.selected is not None else None

    def as_dict(self, *, with_values: bool = True) -> dict[str, Any]:
        """``{"source", "label", "alternatives": [...]}``: the shape every reader returns."""
        return {
            "source": self.selected.source if self.selected else None,
            "label": label(self.selected.source) if self.selected else None,
            "alternatives": [item.as_dict(with_value=with_values) for item in self.alternatives],
        }


def resolve(
    metric: str, observations: Iterable[Observation], policy: Policy | None = None
) -> Resolution:
    """Order the observations of one conflict by the policy of the metric's category.

    Observations without a value (``None``) are left out: a source that measured nothing is
    not an alternative. Two observations of one source keep their given order. Nothing is
    summed: the result holds exactly the observations given.
    """
    policy = policy or Policy.from_settings()
    kind = category(metric)
    ranked = sorted(
        (item for item in observations if item.value is not None),
        key=lambda item: (policy.rank(item.source, kind), item.source),
    )
    if not ranked:
        return Resolution(None)
    return Resolution(ranked[0], tuple(ranked[1:]))


# -- sessions (workouts) ------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPlacement:
    """Where a session stands after resolution: shown first, or kept as an alternative of
    the session (``group``) it overlaps with."""

    priority: str
    group: str | None = None
    alternatives: tuple[Observation, ...] = ()


class _SessionIndex:
    """Start-ordered segment tree for the first overlapping session in O(log n).

    Each node retains the largest ends of two distinct sources. Excluding one source
    still leaves an exact maximum, so even dense same-source inputs prune immediately.
    Removing visited sessions lets a graph search find components without listing edges.
    """

    def __init__(self, items: Mapping[str, Observation], keys: Sequence[str]) -> None:
        self.sessions = [
            (item.start, item.end, item.source, key)
            for key in keys
            if (item := items[key]).start is not None and item.end is not None
        ]
        self.starts = [start for start, _, _, _ in self.sessions]
        self.positions = {key: i for i, (_, _, _, key) in enumerate(self.sessions)}
        self.size = 1 << (len(self.sessions) - 1).bit_length() if self.sessions else 1
        self.tree: list[tuple[tuple[datetime, str], ...]] = [()] * (2 * self.size)
        for i, (_, end, source, _) in enumerate(self.sessions):
            self.tree[self.size + i] = ((end, source),)
        for node in range(self.size - 1, 0, -1):
            self._refresh(node)

    def _refresh(self, node: int) -> None:
        candidates = sorted((*self.tree[2 * node], *self.tree[2 * node + 1]), reverse=True)
        best: list[tuple[datetime, str]] = []
        for end, source in candidates:  # At most four entries, never session pairs.
            if not best or source != best[0][1]:
                best.append((end, source))
            if len(best) == 2:
                break
        self.tree[node] = tuple(best)

    def remove(self, key: str) -> None:
        if key not in self.positions:
            return
        node = self.size + self.positions[key]
        self.tree[node] = ()
        while node > 1:
            node //= 2
            self._refresh(node)

    def first(self, item: Observation, *, other_source: bool = False) -> str | None:
        if item.start is None or item.end is None:
            return None
        start = item.start
        stop = bisect_left(self.starts, item.end)

        def search(node: int, left: int, right: int) -> str | None:
            if left >= stop or not any(
                end > start and (not other_source or source != item.source)
                for end, source in self.tree[node]
            ):
                return None
            if right - left == 1:
                return self.sessions[left][3]
            middle = (left + right) // 2
            found = search(2 * node, left, middle)
            return found if found is not None else search(2 * node + 1, middle, right)

        return search(1, 0, self.size)


def _session_groups(items: Mapping[str, Observation], devices: Sequence[str]) -> list[list[str]]:
    """Visit each cross-source component, removing each session from the index once.

    There are at most n successful and n unsuccessful neighbor queries, including for
    complete overlap graphs. Together with sorting and removals this costs O(n log n).
    """
    index = _SessionIndex(items, devices)
    unseen = set(devices)
    groups = []
    for seed in devices:
        if seed not in unseen:
            continue
        unseen.remove(seed)
        index.remove(seed)
        members = [seed]
        # Appending discoveries visits the component without recursion or queue copies.
        for key in members:
            while (neighbor := index.first(items[key], other_source=True)) is not None:
                index.remove(neighbor)
                unseen.remove(neighbor)
                members.append(neighbor)
        groups.append(members)
    return groups


def resolve_sessions(
    sessions: Sequence[Observation], policy: Policy | None = None
) -> dict[str, SessionPlacement]:
    """Place each device session (``record_id`` -> placement) under the activity policy.

    Sessions of different sources whose times overlap form one conflict group (overlap is
    transitive: a long Garmin run that touches two Fitbit auto-detected walks groups all
    three). In a group the sessions of the best-ranked source present are ``primary``; every
    other session is ``secondary`` and listed as an alternative of the primary it overlaps
    with (the first one, by start time). Sessions of one source never conflict with each
    other, and a ``MANUAL`` session is always primary on its own.
    """
    policy = policy or Policy.from_settings()
    items = {item.record_id: item for item in sessions if item.record_id is not None}

    def order(key: str) -> tuple[datetime, str]:
        return items[key].start or datetime.min, key

    devices = sorted((key for key, item in items.items() if item.source != MANUAL), key=order)
    placements: dict[str, SessionPlacement] = {
        key: SessionPlacement(PRIMARY) for key, item in items.items() if item.source == MANUAL
    }
    for members in _session_groups(items, devices):
        rank = {key: policy.rank(items[key].source, ACTIVITY) for key in members}
        best = min(rank.values())
        primaries = sorted((key for key in members if rank[key] == best), key=order)
        held: dict[str, list[Observation]] = {key: [] for key in primaries}
        primary_index = _SessionIndex(items, primaries)
        for key in sorted((key for key in members if rank[key] != best), key=order):
            holder = primary_index.first(items[key])
            if holder is None:
                holder = primaries[0]
            held[holder].append(items[key])
            placements[key] = SessionPlacement(SECONDARY, holder)
        for primary in primaries:
            placements[primary] = SessionPlacement(PRIMARY, None, tuple(held[primary]))
    return placements


# -- samples (minute series) --------------------------------------------------------------


def sample_category(moment: datetime, windows: Iterable[tuple[datetime, datetime]]) -> str:
    """``activity`` for a sample inside a selected workout window, else ``health``."""
    return ACTIVITY if any(start <= moment < end for start, end in windows) else HEALTH


def _sample_slices(
    series: Mapping[str, Sequence[tuple[datetime, float]]],
    boundaries: Sequence[datetime],
) -> dict[tuple[datetime, int], dict[str, list[tuple[datetime, float]]]]:
    """Partition UTC minutes at exact window boundaries, keeping original samples.

    The boundary index from ``bisect_right`` puts a sample exactly at a boundary in
    the slice starting there, so starts are inclusive and ends are exclusive.
    """
    slices: dict[tuple[datetime, int], dict[str, list[tuple[datetime, float]]]] = {}
    for source, samples in series.items():
        for moment, value in samples:
            minute = moment.astimezone(UTC).replace(second=0, microsecond=0)
            slice_index = bisect_right(boundaries, moment)
            candidates = slices.setdefault((minute, slice_index), {})
            candidates.setdefault(source, []).append((moment, value))
    return slices


def resolve_samples(
    series: Mapping[str, Sequence[tuple[datetime, float]]],
    windows: Sequence[tuple[datetime, datetime]] = (),
    policy: Policy | None = None,
) -> tuple[list[tuple[datetime, float, str]], dict[str, int]]:
    """Select one source per UTC minute slice (source code -> original samples).

    Exact workout starts and ends split a minute into half-open slices. Only sources
    present in a slice compete under its activity/health policy; all of the winner's
    samples in that slice survive. Minutes without boundaries keep their usual selection.
    Returns time-ordered samples and each source's number of distinct contributing UTC
    minutes. A source contributing several slices of one minute counts only once.
    """
    policy = policy or Policy.from_settings()
    boundaries = sorted(
        {bound for start, end in windows if start < end for bound in (start, end)},
    )
    slices = _sample_slices(series, boundaries)
    kept: list[tuple[datetime, float, str]] = []
    chosen: dict[str, set[datetime]] = {}
    for (minute, _), candidates in sorted(slices.items()):
        # Every sample in this slice has the same category, even in overlapping windows.
        moment = next(iter(candidates.values()))[0][0]
        kind = sample_category(moment, windows)
        source = min(candidates, key=lambda code: (policy.rank(code, kind), code))
        chosen.setdefault(source, set()).add(minute)
        samples = sorted(candidates[source])
        kept.extend((moment, value, source) for moment, value in samples)
    return kept, {source: len(minutes) for source, minutes in chosen.items()}
