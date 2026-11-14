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
            reads.extend(
                await archive.daily(
                    session, window, calendar.end_date, calendar.zone, calendar.sources
                )
            )
    step_hours: list[float | None] = [None] * 24
    for day, values in rows.items():
        for metric in calendar.sources.metrics:
            candidates = [
                (read.days[day][metric], read) for read in reads if metric in read.days.get(day, {})
            ]
            # Retain each observation's originating read: equal source codes can
            # still belong to different providers with different hourly evidence.
            observations = [
                (Observation(item.source, item.value, provider=item.provider), read)
                for item, read in candidates
            ]
            resolution = resolve(metric, [item for item, _ in observations], policy)
            selected_read = next(
                (read for item, read in observations if item is resolution.selected), None
            )
            values[metric] = resolution.value
            values["sources"][metric] = resolution.as_dict()
            if day == calendar.end_date and metric == "steps" and selected_read is not None:
                step_hours = (
                    selected_read.step_hours
                    if selected_read.step_hours is not None
                    else [None] * 24
                )
            if metric == "sleep_seconds" and candidates:
                selected = (
                    selected_read.days[day][metric]
                    if selected_read is not None
                    else candidates[0][0]
                )
                values["sleep_assumptions"] = list(selected.sleep_assumptions)
    return step_hours


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
