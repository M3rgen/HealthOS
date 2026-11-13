"""Resolve typed provider day observations without combining their values."""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.dashboard import DashboardDay
from app.services.identities.source_precedence import Observation, Policy, resolve
from app.services.readers import Archives, CalendarRead, DailyRead, DashboardWindow


async def read_days(
    session: AsyncSession,
    window: DashboardWindow,
    policy: Policy,
    rows: dict[date, DashboardDay],
    archives: Archives,
    *,
    calendar: CalendarRead,
) -> list[float | None]:
    """Resolve ``calendar.sources.metrics`` of ``rows``; only their records are read."""
    reads: list[DailyRead] = []
    for archive in archives:
        if archive.daily is not None:
            reads.append(
                await archive.daily(
                    session, window, calendar.end_date, calendar.zone, calendar.sources
                )
            )
    for day, values in rows.items():
        for metric in calendar.sources.metrics:
            candidates = [
                read.days[day][metric] for read in reads if metric in read.days.get(day, {})
            ]
            resolution = resolve(
                metric,
                [
                    Observation(item.source, item.value, provider=item.provider)
                    for item in candidates
                ],
                policy,
            )
            values[metric] = resolution.value
            values["sources"][metric] = resolution.as_dict()
            if metric == "sleep_seconds" and candidates:
                selected = next(
                    (
                        item
                        for item in candidates
                        if resolution.selected is not None
                        and item.source == resolution.selected.source
                        and item.provider == resolution.selected.provider
                    ),
                    candidates[0],
                )
                values["sleep_assumptions"] = list(selected.sleep_assumptions)
    return next((read.step_hours for read in reads if read.step_hours is not None), [None] * 24)


def set_status(rows: dict[date, DashboardDay]):
    for values in rows.values():
        present = sum(
            values[key] is not None
            for key in ("steps", "sleep_seconds", "resting_bpm", "hrv_ms", "spo2")
        )
        values["status"] = (
            "empty"
            if not present
            else "partial"
            if values["partial"]
            else "complete"
            if present == 5
            else "missing"
        )
