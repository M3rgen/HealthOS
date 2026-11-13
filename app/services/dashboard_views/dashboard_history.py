"""Archive bounds and thirty-day dashboard history pages."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.dashboard import History
from app.services.identities.source_precedence import Policy
from app.services.providers.ingestion_readers import ARCHIVES
from app.services.readers import (
    Archives,
    CalendarRead,
    DashboardSources,
    HistoryMetric,
    checked_dates,
    daily_window,
    day_rows,
)

from .dashboard_daily import read_days


class HistoryPageNotFound(ValueError):
    """The requested page lies outside the valid archive."""


async def history_data(
    session: AsyncSession,
    metric: HistoryMetric,
    page: int,
    timezone: str,
    today: date,
    *,
    archives: Archives = ARCHIVES,
) -> History:
    zone = ZoneInfo(timezone)
    with checked_dates():
        end_date = today - timedelta(days=30 * (page - 1))
    # Even the oldest page is a full thirty-day window, not a clipped archive tail.
    window = daily_window(end_date, 30, zone)
    value_key = {"steps": "steps", "sleep": "sleep_seconds", "heart": "resting_bpm"}[metric]
    bounds = [
        await archive.archive_bounds(session, metric, today, zone)
        for archive in archives
        if archive.archive_bounds is not None
    ]
    earliest_date = min(
        (item.earliest for item in bounds if item.earliest is not None), default=None
    )
    latest_date = max((item.latest for item in bounds if item.latest is not None), default=None)
    total_pages = ((today - earliest_date).days // 30 + 1) if earliest_date else 1
    if page > total_pages:
        raise HistoryPageNotFound("Bu geçmiş sayfası bulunamadı.")
    # The values the dashboard shows for these days, under the same precedence policy, read
    # for this metric alone (never the profile, the charts or the other metrics).
    rows, _ = day_rows(window.first, 30, today)
    await read_days(
        session,
        window,
        Policy.from_settings(),
        rows,
        archives,
        calendar=CalendarRead(
            end_date=end_date, zone=zone, sources=DashboardSources(metrics=(value_key,))
        ),
    )
    return {
        "metric": metric,
        "page": page,
        "total_pages": total_pages,
        "start_date": window.first,
        "end_date": end_date,
        "earliest_date": earliest_date,
        "latest_date": latest_date,
        "timezone": timezone,
        "days": [
            {"date": row["date"], "value": row[value_key], "partial": row["partial"]}
            for row in reversed(rows.values())
        ],
    }
