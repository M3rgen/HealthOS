"""Coverage card assembly from independent presence and retrieval readers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.dashboard import DashboardDay
from app.services.archive.manual_flags import excluded_measurement_ids
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.readers import DashboardQuery

from .coverage_reading import (
    COVERAGE_BUDGET_BYTES,
    METRICS,
    CoverageCard,
    Exclusions,
    MetricCoverage,
    Retrieval,
    Sampling,
    Stream,
    google_streams,
    manual_stream,
    metric_exclusions,
    source_regime,
)
from .coverage_reading import EXCLUSION_POLICY as EXCLUSION_POLICY
from .coverage_retrieval import metric_retrieval
from .coverage_types import Metric as Metric
from .coverage_types import Window
from .retrieval_coverage import receipt_horizon


@dataclass(frozen=True)
class _CoverageContext:
    dashboard_days: dict[date, DashboardDay]
    excluded: set[UUID]
    horizon: datetime | None
    single_metric: bool
    closed_days: int


async def _metric_coverage(
    session: AsyncSession, metric: Metric, window: Window, context: _CoverageContext
) -> MetricCoverage:
    streams: dict[str, Stream] = {}
    invalid: int | None = None
    manual_dropped = 0
    codes = ["presence_only"]
    if metric.data_type is not None:
        streams, invalid = await google_streams(session, metric, window)
    if metric.manual is not None:
        stream, dropped = await manual_stream(session, metric, window, context.excluded)
        streams["manuel"] = stream
        manual_dropped = dropped
    if metric.read in {"steps", "sleep", "weight"}:
        invalid = None  # rows that never normalized are not looked for yet
        codes.append("invalid_shape_unknown")
    if metric.read in {"steps", "sleep"}:
        days = {day for day, row in context.dashboard_days.items() if row[metric.key] is not None}
    else:
        # Presence is existential: another invalid row on the same day must
        # not erase a valid source value, regardless of dashboard selection.
        days = set().union(*(stream.days for stream in streams.values()))
    regimes = [
        regime
        for regime in (source_regime(stream, window) for stream in streams.values())
        if regime is not None
    ]
    regimes.sort(key=lambda regime: (regime.first_date, regime.source))
    value_days = sum(day < window.today for day in days)
    partial_today = window.today in days
    retrieval, retrieval_codes = await metric_retrieval(
        session, metric, window, days, context.horizon, gaps=context.single_metric
    )
    codes += retrieval_codes
    if not value_days and not partial_today:
        codes.append("no_value_in_window")
    if len(regimes) > 1:
        codes.append("separate_source_streams")
    return MetricCoverage(
        metric_key=metric.key,
        data_type=metric.data_type,
        value_days=value_days,
        days_without_value=context.closed_days - value_days,
        partial_today=partial_today,
        sampling=Sampling(sample_count=sum(regime.record_count for regime in regimes)),
        retrieval=Retrieval(**retrieval),
        exclusions=Exclusions(
            **await metric_exclusions(session, metric, window),
            manually_excluded=manual_dropped,
            invalid_shape=invalid,
        ),
        source_regimes=regimes,
        reason_codes=codes,
    )


async def coverage_card(
    session: AsyncSession,
    start_date: date,
    end_date: date,
    timezone: str,
    data_type: str | None = None,
) -> CoverageCard:
    zone = ZoneInfo(timezone)
    window = Window(start_date, end_date, datetime.now(zone).date(), zone)
    horizon = await receipt_horizon(session)
    metrics = [
        metric
        for metric in METRICS
        if data_type is None or data_type in (metric.key, metric.data_type, metric.manual)
    ]
    if not metrics:
        raise ValueError(f"coverage için bilinmeyen data_type: {data_type}")
    requested = (end_date - start_date).days + 1
    closed = sum(start_date + timedelta(days=i) < window.today for i in range(requested))
    dashboard_days: dict[date, DashboardDay] = {}
    if any(metric.read in {"steps", "sleep"} for metric in metrics):
        data = await dashboard_data(session, query=DashboardQuery(end_date, requested, timezone))
        dashboard_days = {date.fromisoformat(row["date"]): row for row in data["days"]}
    excluded = await excluded_measurement_ids(session)
    context = _CoverageContext(dashboard_days, excluded, horizon, len(metrics) == 1, closed)
    cards = [await _metric_coverage(session, metric, window, context) for metric in metrics]
    codes = ["daily_status_not_coverage"]
    if horizon is None:
        codes.insert(0, "retrieval_receipts_unavailable")
    elif len(metrics) > 1:
        codes.append("retrieval_gaps_need_data_type")
    if closed < requested:
        codes.append("today_partial_excluded")
    return CoverageCard(
        start_date=start_date,
        end_date=end_date,
        timezone=timezone,
        computed_at=datetime.now(UTC),
        requested_days=requested,
        closed_days=closed,
        retrieval_receipts_since=horizon,
        metrics=cards,
        reason_codes=codes,
    )


async def coverage_response(
    session: AsyncSession,
    start_date: date,
    end_date: date,
    timezone: str,
    data_type: str | None = None,
) -> dict:
    """The card as JSON, or a structured error: an unknown ``data_type`` or a card over the
    budget (never a clipped card)."""
    accepted = sorted({name for m in METRICS for name in (m.key, m.data_type, m.manual) if name})
    if data_type is not None and data_type not in accepted:
        return {
            "error": "coverage için data_type bir metrik ya da veri türü olmalı; "
            "verilmezse bütün metrikler döner.",
            "code": "coverage_unknown_data_type",
            "data_type": data_type,
            "accepted_data_types": accepted,
        }
    card = await coverage_card(session, start_date, end_date, timezone, data_type)
    result = card.model_dump(mode="json")
    size = len(json.dumps(result, ensure_ascii=False).encode())
    if size <= COVERAGE_BUDGET_BYTES:
        return result
    days = (end_date - start_date).days + 1
    return {
        "error": "Kapsama yanıtı 12 KiB sınırını aşıyor; daha dar bir tarih aralığı ya da "
        "data_type ile tek bir metrik iste.",
        "code": "coverage_budget_exceeded",
        "budget_bytes": COVERAGE_BUDGET_BYTES,
        "size_bytes": size,
        "suggested_range": {
            "start_date": (end_date - timedelta(days=max(days // 2, 1) - 1)).isoformat(),
            "end_date": end_date.isoformat(),
        },
        "metric_keys": [metric["metric_key"] for metric in result["metrics"]],
    }
