"""Public dashboard views service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        dashboard,
        dashboard_daily,
        dashboard_history,
        dashboard_profile,
        dashboard_workouts,
        devices,
    )

__all__ = [
    "dashboard",
    "dashboard_daily",
    "dashboard_history",
    "dashboard_profile",
    "dashboard_workouts",
    "devices",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
