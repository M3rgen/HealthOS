"""Provider-neutral read capabilities and projection values; no clients or credentials."""

# pyright: strict

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from .dashboard_sources import DashboardSources
from .dashboard_window import DashboardWindow


class WorkoutRecord(TypedDict):
    id: str
    exercise_type: str | None
    start_time: datetime
    end_time: datetime
    duration_seconds: float | None
    calories_kcal: float | None
    distance_meters: float | None
    source_code: str
    provider: str


@dataclass(frozen=True)
class DailyValue:
    value: float | None
    source: str
    provider: str
    sleep_assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DailyRead:
    days: dict[date, dict[str, DailyValue]]
    step_hours: list[float | None] | None = None


@dataclass(frozen=True)
class HeartRead:
    samples: list[tuple[datetime, float, str]]
    summary_source: str | None
    # Some archives provide workout windows for minute-level precedence. Without those
    # observations a sole stream keeps its historical hourly aggregation and metadata.
    resolve_minutes: bool = False
    activity_windows: list[tuple[datetime, datetime]] = field(
        default_factory=list[tuple[datetime, datetime]]
    )
    activity_source: str | None = None


class HeartReader(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        start: datetime,
        stop: datetime,
        source_filter: ColumnElement[bool],
    ) -> HeartRead: ...


class WorkoutReader(Protocol):
    async def __call__(
        self, session: AsyncSession, start: datetime, stop: datetime
    ) -> list[WorkoutRecord]: ...


class DailyReader(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        window: DashboardWindow,
        end_date: date,
        zone: ZoneInfo,
        sources: DashboardSources,
    ) -> DailyRead: ...


type HistoryMetric = Literal["steps", "sleep", "heart"]


@dataclass(frozen=True)
class ArchiveBounds:
    earliest: date | None = None
    latest: date | None = None


class BoundsReader(Protocol):
    async def __call__(
        self, session: AsyncSession, metric: HistoryMetric, today: date, zone: ZoneInfo
    ) -> ArchiveBounds: ...


@dataclass(frozen=True)
class ArchiveReaders:
    """Unsupported capabilities are absent, rather than names in an untyped catalog."""

    name: str
    heart_rate: HeartReader | None = None
    workouts: WorkoutReader | None = None
    daily: DailyReader | None = None
    archive_bounds: BoundsReader | None = None


type Archives = Sequence[ArchiveReaders]
