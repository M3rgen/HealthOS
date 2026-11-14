"""Civil-day dashboard read model; never modifies source health records."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.health import HeartRate
from app.models.identity_queries import plan_each_query
from app.schemas.dashboard import Dashboard
from app.services.identities.source_precedence import (
    Policy,
    label,
    resolve_samples,
)
from app.services.providers.ingestion_readers import ARCHIVES
from app.services.readers import (
    ArchiveReaders,
    CalendarRead,
    DashboardQuery,
    DashboardSources,
    daily_window,
    day_rows,
)
from app.services.shared.finite_math import finite_mean

from .dashboard_daily import (
    read_days,
    set_status,
)
from .dashboard_profile import dashboard_profile


async def dashboard_data(
    session: AsyncSession,
    *,
    valid_sources_only: bool = False,
    include_source_deleted: bool = False,
    policy: Policy | None = None,
    archives: Sequence[ArchiveReaders] = ARCHIVES,
    query: DashboardQuery,
) -> Dashboard:
    """The civil-day read model: per day the five values, each the *selected* source's value
    under the precedence policy (app/services/identities/source_precedence.py) with the other
    sources'
    values kept as ``sources[metric].alternatives``; never a sum across sources."""
    policy = policy or Policy.from_settings()
    zone = ZoneInfo(query.timezone)
    window = daily_window(query.end_date, query.days, zone)
    await plan_each_query(session)
    rows, _ = day_rows(window.first, query.days, datetime.now(zone).date())
    sources = DashboardSources(include_source_deleted and not valid_sources_only)
    step_hours = await read_days(
        session,
        window,
        policy,
        rows,
        archives,
        calendar=CalendarRead(end_date=query.end_date, zone=zone, sources=sources),
    )
    heart_hours, heart_sources = await _heart_chart(
        session, query.end_date, zone, sources.normalized(HeartRate, "heart-rate"), policy, archives
    )
    profile = await dashboard_profile(session, window.stop, sources)
    set_status(rows)
    return {
        "timezone": query.timezone,
        "start_date": window.first,
        "end_date": query.end_date,
        "days": list(reversed(rows.values())),
        "profile": profile,
        "charts": {
            "steps": step_hours,
            "heart_rate": heart_hours,
            "sources": {
                "steps": _chart_source(rows[query.end_date]["sources"]["steps"]),
                "heart_rate": heart_sources,
            },
        },
        "precedence": policy.as_dict(),
        "sleep_method": (
            "Uyanılan güne atanır; uyanık ve huzursuz süre çıkarılır, evre bilgisi yoksa "
            "süre üst sınırdır. Örtüşen oturumlar tek sayılır; örtüşmede süre tahminidir. "
            "Günün varsayımları sleep_assumptions alanındadır."
        ),
    }


def _chart_source(source: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key != "alternatives"} | {
        "alternatives": [
            {key: value for key, value in item.items() if key != "value"}
            for item in source["alternatives"]
        ]
    }


async def _heart_chart(
    session: AsyncSession,
    day: date,
    zone: ZoneInfo,
    source_filter: ColumnElement[bool],
    policy: Policy,
    archives: Sequence[ArchiveReaders],
) -> tuple[list[float | None], dict[str, Any]]:
    """The day's hourly means of original samples selected per minute slice: inside a
    selected workout window the activity policy picks (Garmin first), outside it the
    health policy (Fitbit first). Exact window boundaries split minutes before selection;
    hourly means use all surviving samples without first averaging sources or slices.
    A source code read from a later archive fills only the minutes an earlier archive left
    empty for that code (Apple's HEALTH_KIT samples, which Google also relays, are not
    counted twice). Returns the 24 means and the sources card (``source``, ``label``,
    ``alternatives``, ``activity_windows`` and ``minutes_by_source``)."""
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    reads = []
    for archive in archives:
        if archive.heart_rate is not None:
            result = await archive.heart_rate(session, start, stop, source_filter)
            if result.samples:
                reads.append(result)
    # Archive count is not source count: a single reader may carry several platforms.
    # Build the source series before taking the historical single-source shortcut.
    series: dict[str, list[tuple[datetime, float]]] = {}
    for read in reads:
        earlier = {
            source: {moment.replace(second=0, microsecond=0) for moment, _ in samples}
            for source, samples in series.items()
        }
        for moment, bpm, source in read.samples:
            if moment.replace(second=0, microsecond=0) not in earlier.get(source, ()):
                series.setdefault(source, []).append((moment, bpm))
        for source in earlier.keys() & {source for _, _, source in read.samples}:
            series[source].sort()
    if not reads or (len(reads) == 1 and len(series) == 1 and not reads[0].resolve_minutes):
        samples = reads[0].samples if reads else []
        hours = hourly_means([(moment, bpm) for moment, bpm, _ in samples], zone)
        source = reads[0].summary_source if reads else None
        return hours, {
            "source": source,
            "label": _label(source),
            "alternatives": [],
            "activity_windows": None,
        }
    windows = [window for read in reads for window in read.activity_windows]
    kept, chosen = resolve_samples(series, windows, policy)
    hours = hourly_means([(moment, bpm) for moment, bpm, _ in kept], zone)
    ranked = sorted(series, key=lambda code: (policy.rank(code, "health"), code))
    return hours, {
        "source": ranked[0],
        "label": _label(ranked[0]),
        "alternatives": [{"source": code, "label": _label(code)} for code in ranked[1:]],
        "activity_windows": next(
            (read.activity_source for read in reads if read.activity_windows), None
        ),
        "minutes_by_source": chosen,
    }


def hourly_means(samples: list[tuple[datetime, float]], zone: ZoneInfo) -> list[float | None]:
    """Mean per local hour (24 values, None for an hour without a sample)."""
    buckets: dict[int, list[float]] = {}
    for moment, bpm in samples:
        buckets.setdefault(moment.astimezone(zone).hour, []).append(bpm)
    means = [finite_mean(buckets.get(hour, [])) for hour in range(24)]
    return [round(value, 1) if value is not None else None for value in means]


def _label(source: str | None) -> str | None:
    return label(source)
