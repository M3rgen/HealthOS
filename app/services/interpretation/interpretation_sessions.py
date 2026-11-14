"""Evidence-backed Google and manual sessions for Umay's archive reader."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from heapq import heappop, heappush
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.health import ExerciseSession
from app.parsers.common import duration_seconds, payload_for
from app.parsers.text import optional_column_text
from app.services import readers

from .canonical_daily import bounds, finite
from .interpretation_inputs import (
    BudgetError,
    Inputs,
    evidence,
    fallback_source,
    scaled_measurement,
)
from .interpretation_types import SessionSummary, Summary

if TYPE_CHECKING:
    from .archive_query import ArchiveQuery

# Bound references across the whole filtered result, independent of page size.
MAX_OVERLAP_REFERENCES = 20_000


def _exercise_type(payload: Mapping[str, object]) -> str | None:
    """Select before validating, exactly as ingestion and the workout panel do.

    Truthy malformed exerciseType values must not reveal a displayName fallback.
    The mapped nullable text contract preserves Unicode and whitespace verbatim;
    malformed or overlong selected values become absent without changing the raw
    archive. Keep the SQL length in the model rather than a reader-specific limit.
    """
    selected = payload.get("exerciseType") or payload.get("displayName")
    return optional_column_text(ExerciseSession, "exercise_type", selected)


def _reported_active_duration(value: object, delta: timedelta) -> tuple[float | None, list[str]]:
    """Check source precision before float rounding or underflow can hide invalidity.

    Only activeDuration supplies this measurement. Missing, malformed and nonfinite
    inputs retain the existing absent result, without a normalized/interval fallback.
    Build the physical limit from integer components, never float total_seconds().
    """
    active = duration_seconds(value)
    if active is not None and active.is_finite():
        seconds = delta.days * 86400 + delta.seconds
        interval = Decimal(f"{seconds}.{delta.microseconds:06d}")
        if not 0 <= active <= interval:
            return None, ["invalid_reported_active_duration"]
    return finite(active), []


def _session_rows(inputs: Inputs, left: datetime, stop: datetime, timezone: str) -> Iterator[dict]:
    """Yield original intervals that intersect the requested window.

    Filter with source times, retaining sessions starting before the first day.
    A missing label does not make the interval or its finite measurements invalid.
    """
    for raw in inputs.rows:
        if raw.data_type != "exercise":
            continue
        start, end = as_utc(raw.source_start_time), as_utc(raw.source_end_time)
        if start is None or end is None or end <= start or start >= stop or end <= left:
            continue
        payload = payload_for(RecordType("exercise"), raw.raw_json)
        metrics, metric_reasons = readers.current_workouts.nonnegative_metrics(
            payload.get("metricsSummary")
        )
        active, active_reasons = _reported_active_duration(
            payload.get("activeDuration"), end - start
        )
        distance, distance_reasons = scaled_measurement(metrics.get("distanceMillimeters"), 1, 1000)
        # A rejected distance already has a precise cause, rather than being missing.
        if "negative_measurement:distanceMillimeters" not in metric_reasons:
            metric_reasons.extend(distance_reasons)
        yield dict(
            metric="exercise",
            source=inputs.source(
                "exercise",
                raw.raw_json.get("dataSource"),
                fallback_source(raw),
            ),
            definition="google_exercise_session",
            time_basis="physical_interval",
            unit="s",
            start=start.isoformat(),
            end=end.isoformat(),
            interval_seconds=(end - start).total_seconds(),
            reported_active_seconds=active,
            reason_codes=active_reasons,
            exercise_type=_exercise_type(payload),
            calories_kcal=finite(metrics.get("caloriesKcal")),
            distance_meters=distance,
            evidence=[
                evidence(raw, start.astimezone(ZoneInfo(timezone)).date(), reasons=metric_reasons)
            ],
        )
    for item in inputs.workouts:
        start, end = as_utc(item.start_time), as_utc(item.end_time)
        if end <= start or start >= stop or end <= left:
            continue
        yield dict(
            metric="manual_workout",
            source="manuel",
            definition="manual_workout_session",
            time_basis="physical_interval",
            unit="s",
            start=start.isoformat(),
            end=end.isoformat(),
            interval_seconds=(end - start).total_seconds(),
            reported_active_seconds=None,
            exercise_type=item.exercise_type,
            calories_kcal=finite(item.calories_kcal),
            distance_meters=finite(item.distance_meters),
            evidence=[
                dict(
                    record=str(item.id),
                    date=str(start.astimezone(ZoneInfo(timezone)).date()),
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            ],
        )


def _matches(row: dict, query: ArchiveQuery) -> bool:
    return (not query.source or row["source"] == query.source) and (
        not query.definition or row["definition"] == query.definition
    )


def _session_overlaps(rows: list[dict], selected: set[int]) -> dict[int, list[str]]:
    """Sweep sorted intervals in O(n log n + references), including outside sources.

    Only selected sessions need output lists. Ordered dictionaries visit live entries
    in insertion order, avoiding scans of deleted entries after many intervals expire.
    Earlier references followed by later ones retain the original row order.
    Expire touching endpoints before visiting active intervals. Dense unselected
    sessions never enumerate each other; every visit contributes an output reference.
    """
    overlaps: dict[int, list[str]] = {index: [] for index in selected}
    active: OrderedDict[int, str] = OrderedDict()
    active_selected: OrderedDict[int, str] = OrderedDict()
    endings: list[tuple[str, int]] = []
    references = 0
    for index, row in enumerate(rows):
        while endings and endings[0][0] <= row["start"]:
            _, expired = heappop(endings)
            del active[expired]
            active_selected.pop(expired, None)
        included = index in selected
        references += len(active_selected) + (len(active) if included else 0)
        if references > MAX_OVERLAP_REFERENCES:
            raise BudgetError("overlap_budget_exceeded")
        record = row["evidence"][0]["record"]
        for previous in active_selected:
            overlaps[previous].append(record)
        if included:
            overlaps[index].extend(active.values())
            active_selected[index] = record
        active[index] = record
        heappush(endings, (row["end"], index))
    return overlaps


def session_summaries(inputs: Inputs, query: ArchiveQuery, *, max_summaries: int) -> list[Summary]:
    """Read whole sessions intersecting the requested window, retaining source evidence.

    Umay reports physical intervals and reported active time separately; it does not
    infer continuous running or prorate totals to the requested dates. Raw payloads
    are authoritative here even when a correction left an older normalized row.
    Manual workouts retain their own definition, source and interval semantics.

    Count source/definition matches before allocating overlap lists or models.
    The reader supplies its summary ceiling; reference accounting covers the whole
    filtered window even for small pages. Exceeding either budget rejects the whole
    calculation, so callers never receive incomplete overlap evidence.
    """
    first, last = query.date_range()
    left, _ = bounds(first, query.timezone)
    _, stop = bounds(last, query.timezone)
    rows = []
    matched = 0
    for row in _session_rows(inputs, left, stop, query.timezone):
        if _matches(row, query):
            matched += 1
            if matched > max_summaries:
                raise BudgetError("summary_budget_exceeded")
        rows.append(row)
    if not matched:
        return []
    rows.sort(key=lambda row: (row["start"], row["end"], row["evidence"][0]["record"]))
    selected = {index for index, row in enumerate(rows) if _matches(row, query)}
    overlaps = _session_overlaps(rows, selected)
    results: list[Summary] = []
    for index, row in enumerate(rows):
        if index not in selected:
            continue
        reasons = ["continuous_running_unproven", "session_totals_not_additive"]
        reasons.extend(row["evidence"][0].get("reason_codes", []))
        if overlaps[index]:
            reasons.append("overlapping_sessions")
        reasons.extend(row.pop("reason_codes", []))
        results.append(SessionSummary(**row, overlaps=overlaps[index], reason_codes=reasons))
    return results
