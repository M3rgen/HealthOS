"""Read models shared by the dashboard, interpretation, providers and coverage.

Modules and contract names are loaded only when requested.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import (
        current_heart,
        current_sleep,
        current_steps,
        current_weight,
        current_workouts,
        dashboard_manual,
        dashboard_queries,
        dashboard_rows,
        dashboard_sources,
        dashboard_window,
        heart_maintenance,
        heart_projection,
        manual_validation,
        measurement_windows,
        reading,
    )
    from .dashboard_queries import CalendarRead as CalendarRead
    from .dashboard_queries import DashboardQuery as DashboardQuery
    from .dashboard_rows import day_rows as day_rows
    from .dashboard_sources import DashboardSources as DashboardSources
    from .dashboard_window import DashboardWindow as DashboardWindow
    from .dashboard_window import checked_dates as checked_dates
    from .dashboard_window import daily_window as daily_window
    from .heart_maintenance import refresh_heart_hours as refresh_heart_hours
    from .reading import ArchiveReaders as ArchiveReaders
    from .reading import Archives as Archives
    from .reading import DailyRead as DailyRead
    from .reading import HistoryMetric as HistoryMetric

__all__ = [
    "current_heart",
    "heart_projection",
    "heart_maintenance",
    "current_sleep",
    "current_steps",
    "current_weight",
    "current_workouts",
    "dashboard_manual",
    "dashboard_queries",
    "dashboard_rows",
    "dashboard_sources",
    "dashboard_window",
    "manual_validation",
    "measurement_windows",
    "reading",
]
# Names at least three modules outside this package import, mapped to their module. Names with
# fewer importers are imported from their module: each name here is an explicit re-export.
_CONTRACT = {
    "refresh_heart_hours": "heart_maintenance",
    "ArchiveReaders": "reading",
    "Archives": "reading",
    "CalendarRead": "dashboard_queries",
    "DailyRead": "reading",
    "DashboardQuery": "dashboard_queries",
    "DashboardSources": "dashboard_sources",
    "DashboardWindow": "dashboard_window",
    "HistoryMetric": "reading",
    "checked_dates": "dashboard_window",
    "daily_window": "dashboard_window",
    "day_rows": "dashboard_rows",
}


def __getattr__(name: str) -> Any:
    if name in __all__:
        value = import_module(f"{__name__}.{name}")
    elif name in _CONTRACT:
        value = getattr(import_module(f"{__name__}.{_CONTRACT[name]}"), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
