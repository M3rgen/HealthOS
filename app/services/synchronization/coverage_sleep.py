"""Sleep presence uses the dashboard's per-day estimator and source resolution."""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import dashboard_views, identities, providers
from app.services.readers import CalendarRead, DashboardSources, daily_window, day_rows

from .coverage_types import Window


async def sleep_value_days(session: AsyncSession, window: Window) -> set[date]:
    """Read only sleep, for the whole window in fixed queries, without profile/charts.

    The shared Google reader releases payload batches and completed days' overlap
    events as it advances. Only resolved day/platform values survive the window;
    neither a list of archived sessions nor a separate query per day is needed.
    Raw invalid-record counts and source regimes remain the presence reader's job.
    The panel describes one Google data type, so other providers' sleep is not read.
    """
    count = (window.end - window.first).days + 1
    rows, _ = day_rows(window.first, count, window.today)
    await dashboard_views.dashboard_daily.read_days(
        session,
        daily_window(window.end, count, window.zone),
        identities.source_precedence.Policy.from_settings(),
        rows,
        (providers.ingestion_readers.GOOGLE_ARCHIVE,),
        calendar=CalendarRead(
            end_date=window.end,
            zone=window.zone,
            sources=DashboardSources(metrics=("sleep_seconds",)),
        ),
    )
    return {day for day, row in rows.items() if row["sleep_seconds"] is not None}
