"""Calendar selections passed together through dashboard readers."""

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from .dashboard_sources import DashboardSources


@dataclass(frozen=True, slots=True)
class DashboardQuery:
    end_date: date
    days: int
    timezone: str


@dataclass(frozen=True, slots=True)
class CalendarRead:
    end_date: date
    zone: ZoneInfo
    sources: DashboardSources
