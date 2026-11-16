"""Dashboard projections; optional keys preserve create/list and source variants."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, NotRequired, TypedDict


class Measurement(TypedDict):
    id: str
    metric: str
    value: Decimal
    measured_at: datetime
    source: str
    flags: NotRequired[list[str]]


class MeasurementFlag(TypedDict):
    id: str
    flags: list[str]
    changed: bool


class Workout(TypedDict):
    id: str
    exercise_type: str | None
    start_time: datetime
    end_time: datetime
    duration_seconds: float | None
    calories_kcal: float | None
    distance_meters: float | None
    source: str | None
    source_code: str
    provider: str
    priority: str
    group: str | None
    alternatives: list[dict[str, Any]]
    # A ready FIT track exists; the track endpoint takes this row's ``id``.
    has_track: bool


class WorkoutTrackSummary(TypedDict):
    """Seconds, meters and m/s; ``bounds`` is [south, west, north, east] in degrees."""

    start_time: str | None
    elapsed_seconds: float
    moving_seconds: float
    distance: float
    average_speed: float | None
    maximum_speed: float | None
    ascent: float | None
    descent: float | None
    bounds: list[float] | None


class WorkoutTrackBody(TypedDict):
    """Columns are equal-length arrays per field; a channel without data is absent."""

    workout_id: str
    exercise_type: str | None
    method_version: int
    summary: WorkoutTrackSummary
    source_point_count: int
    point_count: int
    samples: dict[str, list[float | None]]
    laps: dict[str, list[float | None]]
    events: dict[str, list[float | str | None]]


class WorkoutTrackView(TypedDict):
    track: WorkoutTrackBody | None


class DashboardDay(TypedDict):
    date: str
    steps: int | float | None
    sleep_seconds: float | None
    sleep_assumptions: list[str]
    resting_bpm: float | None
    hrv_ms: float | None
    spo2: float | None
    partial: bool
    sources: dict[str, Any]
    status: str


class ProfileMeasurement(TypedDict):
    metric: str
    value: float | None
    measured_at: datetime
    source: str
    uncertainties: NotRequired[list[str]]


class Dashboard(TypedDict):
    timezone: str
    start_date: date
    end_date: date
    days: list[DashboardDay]
    profile: dict[str, ProfileMeasurement]
    charts: dict[str, Any]
    precedence: dict[str, Any]
    sleep_method: str


class HistoryDay(TypedDict):
    date: str
    value: int | float | None
    partial: bool


class History(TypedDict):
    metric: str
    page: int
    total_pages: int
    start_date: date
    end_date: date
    earliest_date: date | None
    latest_date: date | None
    timezone: str
    days: list[HistoryDay]
