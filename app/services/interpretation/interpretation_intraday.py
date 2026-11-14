"""Duration-aware intraday cells and existing, evidence-only sleep sessions."""

from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from app.core.normalization import first_number
from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.models.state import RawHealthData
from app.parsers.common import number, payload_for
from app.services.shared.finite_math import finite_calculation, finite_scale

from .archive_query import ArchiveQuery
from .canonical_daily import bounds, finite
from .interpretation_cells import CellPlan, IntradayBudget
from .interpretation_inputs import (
    Inputs,
    evidence,
    fallback_source,
    method_details,
    method_key,
)
from .interpretation_stats import statistics, statistics_reasons
from .interpretation_types import Intraday, SleepSummary

MAX_HEART_GAP_SECONDS = 300


@dataclass
class _Candidate:
    raw: RawHealthData
    start: datetime
    end: datetime | None
    value: float | None
    field: str
    codes: list[str]


def sleep_summaries(derived: dict[str, Any], first: date, last: date) -> list[SleepSummary]:
    results = []
    for day in derived["days"]:
        if not str(first) <= day["date"] <= str(last) or not day.get("result"):
            continue
        for stream in day["result"]["streams"]:
            if stream["metric"] != "sleep_seconds" or stream.get("sleep") is None:
                continue
            results.append(
                SleepSummary.model_validate(
                    dict(
                        metric="sleep_seconds",
                        source=stream["source"],
                        definition=stream["definition"],
                        method_details=method_details({}, stream["source_metadata"]),
                        unit="s",
                        time_basis="wake_day",
                        date=day["date"],
                        value=stream["value"],
                        sleep=stream["sleep"],
                        reason_codes=sorted(
                            set(stream["uncertainties"] + stream["sleep"]["reason_codes"])
                        ),
                        evidence=[
                            dict(
                                record=r["record"],
                                date=day["date"],
                                value=r["value"],
                                start=r["source_start"],
                                end=r["source_end"],
                                source_timezone=r["source_timezone"],
                                reason_codes=r.get("assumptions") or [],
                            )
                            for r in stream["observations"]
                        ],
                    )
                )
            )
    return results


def _intraday_plans(inputs: Inputs, query: ArchiveQuery, budget: IntradayBudget):
    first, last = query.date_range()
    groups = defaultdict(list)
    for raw in inputs.rows:
        kind = raw.data_type
        if kind not in {"steps", "heart-rate"} or (query.data_type and kind != query.data_type):
            continue
        payload = payload_for(RecordType(kind), raw.raw_json)
        metadata = raw.raw_json.get("dataSource")
        source = inputs.source(
            kind,
            metadata,
            fallback_source(raw),
        )
        method = method_key(payload, metadata)
        definition = (
            "linear_sample_interpolation:" if kind == "heart-rate" else "interval_rate:"
        ) + method
        if (query.source and source != query.source) or (
            query.definition and definition != query.definition
        ):
            continue
        key = (kind, source, method)
        groups[key].append(raw)
    plans = []
    windows = [
        bounds(first + timedelta(days=offset), query.timezone)
        for offset in range((last - first).days + 1)
    ]
    starts, stops = zip(*windows, strict=True)
    for (kind, source, method), rows in sorted(groups.items()):
        for offset, candidates in _candidate_days(kind, rows, starts, stops):
            day = first + timedelta(days=offset)
            left, stop, plan, reasons = _day_plan(kind, candidates, day, query.timezone, budget)
            plans.append((kind, source, method, rows[0], day, left, stop, plan, reasons))
    return plans


def _candidate_days(
    kind: str, rows: list[RawHealthData], starts: tuple[datetime, ...], stops: tuple[datetime, ...]
):
    # Index each selected record once. Two events per record keep memory bounded
    # even for long intervals; evidence amplification is charged by _day_plan.
    events: dict[int, list[tuple[int, _Candidate | None]]] = defaultdict(list)
    margin = timedelta(seconds=MAX_HEART_GAP_SECONDS)
    for order, raw in enumerate(rows):
        a, b = as_utc(raw.source_start_time), as_utc(raw.source_end_time)
        if a is None:
            continue
        if kind == "heart-rate":
            # left - 300 <= sample < stop + 300 (inclusive on the left only).
            # Reject distant read-window neighbors before shifting a timestamp
            # that could be at datetime.min. Query bounds already allow this margin.
            if not starts[0] - margin <= a < stops[-1] + margin:
                continue
            first = bisect_right(stops, a - margin)
            stop = bisect_right(starts, a + margin)
        elif b is not None and b > a:
            first, stop = bisect_right(stops, a), bisect_left(starts, b)
        else:
            continue
        if first >= stop:
            continue
        value, field, codes = _intraday_measurement(
            kind, payload_for(RecordType(kind), raw.raw_json)
        )
        events[first].append((order, _Candidate(raw, a, b, value, field, codes)))
        events[stop].append((order, None))

    active: OrderedDict[int, _Candidate] = OrderedDict()
    for first, stop in pairwise(sorted(events)):
        for order, candidate in events[first]:
            if candidate is None:
                del active[order]
            else:
                active[order] = candidate
        if not active:
            continue
        # Restore input order for evidence, statistics and floating-point sums.
        # Reuse it until membership changes; skip empty date ranges altogether.
        candidates = [active[order] for order in sorted(active)]
        for offset in range(first, stop):
            yield offset, candidates


def intraday_summaries(
    inputs: Inputs, query: ArchiveQuery, *, max_summaries: int
) -> list[Intraday]:
    # Finish the whole-query preflight before producing any cells, evidence dicts
    # or models. A small page never hides work or truncates scientific evidence.
    plans = _intraday_plans(inputs, query, IntradayBudget(max_summaries))
    results = []
    for kind, source, method, raw, day, left, stop, plan, reasons in plans:
        observations = []
        for args in plan.observations:
            observations.append(evidence(*args))
        cells, supported, conflict, weighted = [], 0.0, 0.0, 0.0
        weights = []
        for a, b, state, value in plan.cells():
            seconds = (b - a).total_seconds()
            if value is not None:
                supported += seconds
                weighted += value * seconds
                weights.append((value, seconds))
            if state == "conflict":
                conflict += seconds
            cells.append(dict(start=a.isoformat(), end=b.isoformat(), state=state, value=value))
        duration = (stop - left).total_seconds()
        if conflict:
            reasons.append("overlapping_intervals")
        mean = (
            finite_calculation(
                weighted / supported,
                lambda weights=weights, supported=supported: (
                    sum((Fraction(v) * Fraction(s) for v, s in weights), Fraction())
                    / Fraction(supported)
                ),
            )
            if supported
            else None
        )
        if supported and mean is None:
            reasons.append("numeric_overflow:time_weighted_mean")
        stats = statistics(
            [
                r["value"]
                for r in observations
                if r["value"] is not None and (kind == "steps" or r["date"] == str(day))
            ]
        )
        reasons.extend(statistics_reasons(stats))
        reasons.extend(code for row in observations for code in row["reason_codes"])
        results.append(
            Intraday(
                metric=kind,
                source=source,
                method_details=method_details(
                    payload_for(RecordType(kind), raw.raw_json),
                    raw.raw_json.get("dataSource"),
                ),
                definition=(
                    "linear_sample_interpolation:" if kind == "heart-rate" else "interval_rate:"
                )
                + method,
                unit="bpm" if kind == "heart-rate" else "count/s",
                time_basis="physical_seconds",
                date=str(day),
                day_seconds=duration,
                supported_seconds=supported,
                gap_seconds=duration - supported - conflict,
                conflict_seconds=conflict,
                cells=cells,
                time_weighted_mean=mean,
                statistics=stats,
                statistics_unit="bpm" if kind == "heart-rate" else "count",
                statistics_basis="samples_inside_day"
                if kind == "heart-rate"
                else "source_interval_counts",
                evidence=observations,
                reason_codes=sorted(set(reasons)),
            )
        )
    return results


def _intraday_measurement(kind: str, payload: dict) -> tuple[float | None, str, list[str]]:
    """Select a parsed field before checking whether it is a usable measurement.

    Match the current step/heart readers: missing, text and boolean fields can
    fall through, but a parsed NaN, infinity or float overflow cannot borrow an
    alias. Retained normalized columns are never measurement fallbacks.
    Keep null evidence for an invalid value; heart samples must still interrupt
    interpolation, while step intervals must not become supported movement.
    """
    fields = (
        ("count", "countSum", "steps") if kind == "steps" else ("beatsPerMinute", "bpm", "value")
    )
    parsed = first_number(payload, fields, parse=number)
    # Counts and heart rates must be nonnegative. Check the selected Decimal before
    # float conversion so tiny negatives cannot become measured signed zero.
    # Keep null evidence and its timestamp to interrupt heart interpolation on both
    # sides; a positive alias must not hide this invalid sample.
    if parsed is not None and parsed.is_finite() and parsed < 0:
        parsed = None
    value = finite(parsed)
    codes = ["unsupported_or_missing_value"] if value is None else []
    # Preserve the canonical evidence field, including when an alias supplies zero.
    return value, fields[0], codes


def _day_plan(kind: str, rows: list[_Candidate], day: date, timezone: str, budget: IntradayBudget):
    left, stop = bounds(day, timezone)
    plan, samples = CellPlan(left, stop, budget), []
    reasons = ["coverage_is_not_retrieval_completeness"]
    for candidate in rows:
        raw, a, b = candidate.raw, candidate.start, candidate.end
        value, field, codes = candidate.value, candidate.field, list(candidate.codes)
        if kind == "heart-rate":
            plan.observe(raw, a.astimezone(ZoneInfo(timezone)).date(), value, field, codes)
            samples.append((a, value))
        elif b is not None:
            rate = finite_scale(value, 1, (b - a).total_seconds()) if value is not None else None
            if value is not None and rate is None:
                codes.append("numeric_overflow:interval_rate")
            reasons.extend(codes)
            plan.observe(raw, day, value, field, codes)
            plan.add_span(max(a, left), min(b, stop), rate)
    if kind == "heart-rate":
        _interpolate_samples(samples, left, stop, plan, reasons)
    else:
        reasons += [
            "uniform_interval_allocation_estimate",
            "exact_movement_bursts_unavailable",
        ]
    return left, stop, plan, reasons


def _interpolate_samples(
    samples: list[tuple[datetime, float | None]],
    left: datetime,
    stop: datetime,
    plan: CellPlan,
    reasons: list[str],
) -> None:
    reasons += [
        "interpolated_between_samples_only",
        "no_extrapolation",
        "max_interpolation_gap_300_seconds",
    ]
    # Conflicting same-time samples block interpolation on both sides.
    by_time = defaultdict(list)
    for stamp, value in samples:
        by_time[stamp].append(value)
    if any(len(set(values)) > 1 for values in by_time.values()):
        reasons.append("conflicting_same_time_samples")
    points = [
        (stamp, values[0] if len(set(values)) == 1 else None)
        for stamp, values in sorted(by_time.items())
    ]
    for (a, x), (b, y) in pairwise(points):
        if (
            0 < (b - a).total_seconds() <= MAX_HEART_GAP_SECONDS
            and x is not None
            and y is not None
            and a < stop
            and b > left
        ):
            p, q = max(a, left), min(b, stop)
            # Integrate the linear segment on the actual clipped interval.
            mean = (
                x
                + (y - x)
                * (((p - a).total_seconds() + (q - a).total_seconds()) / 2)
                / (b - a).total_seconds()
            )
            elapsed = ((p - a).total_seconds() + (q - a).total_seconds()) / 2
            mean = finite_calculation(
                mean,
                lambda x=x, y=y, elapsed=elapsed, a=a, b=b: (
                    Fraction(x)
                    + (Fraction(y) - Fraction(x))
                    * Fraction(elapsed)
                    / Fraction((b - a).total_seconds())
                ),
            )
            if mean is None:
                reasons.append("numeric_overflow:interpolation")
            plan.add_span(p, q, mean)
