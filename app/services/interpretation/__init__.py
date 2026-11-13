"""Public interpretation service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        archive_query,
        canonical_daily,
        canonical_sleep,
        canonical_types,
        derived,
        google_daily_readers,
        health_interpretation,
        health_windows,
        interpretation_baseline_index,
        interpretation_cells,
        interpretation_data,
        interpretation_input_state,
        interpretation_inputs,
        interpretation_intraday,
        interpretation_measures,
        interpretation_relationship_plan,
        interpretation_series,
        interpretation_sessions,
        interpretation_stats,
        interpretation_types,
        sleep_estimate,
        sleep_timeline,
    )

__all__ = [
    "archive_query",
    "canonical_daily",
    "canonical_sleep",
    "canonical_types",
    "derived",
    "google_daily_readers",
    "health_interpretation",
    "health_windows",
    "interpretation_baseline_index",
    "interpretation_cells",
    "interpretation_data",
    "interpretation_input_state",
    "interpretation_inputs",
    "interpretation_intraday",
    "interpretation_measures",
    "interpretation_relationship_plan",
    "interpretation_series",
    "interpretation_sessions",
    "interpretation_stats",
    "interpretation_types",
    "sleep_estimate",
    "sleep_timeline",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
