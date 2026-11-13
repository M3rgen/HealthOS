"""Value types shared by coverage presence and retrieval readers."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.core.timeutil import as_utc


@dataclass(frozen=True)
class Metric:
    key: str  # the dashboard's day field or profile metric
    data_type: str | None  # Google data type
    read: Literal["steps", "sleep", "raw_daily", "weight", "raw_sample", "manual"]
    fields: tuple[str, ...] = ()  # payload fields of a raw read, as the dashboard reads them
    manual: str | None = None  # manual_measurements.metric


@dataclass
class Window:
    first: date
    end: date
    today: date
    zone: ZoneInfo

    @property
    def start(self) -> datetime:
        return datetime.combine(self.first, time.min, self.zone).astimezone(UTC)

    @property
    def stop(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), time.min, self.zone).astimezone(UTC)

    def local(self, instant: datetime) -> date:
        return as_utc(instant).astimezone(self.zone).date()
