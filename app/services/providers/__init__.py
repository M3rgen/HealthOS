"""Public providers service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        apple_archive,
        apple_import,
        apple_normalize,
        apple_readers,
        credentials,
        demo_archive,
        demo_data,
        discovery,
        fit_decode,
        fit_import,
        fit_records,
        garmin_activity_identity,
        garmin_activity_state,
        garmin_bounded,
        garmin_checkpoints,
        garmin_current,
        garmin_daily_state,
        garmin_health,
        garmin_ingestion,
        garmin_kinds,
        garmin_membership,
        garmin_normalization,
        garmin_normalize,
        garmin_normalize_types,
        garmin_normalize_writing,
        garmin_pull,
        garmin_readers,
        garmin_time,
        garmin_versions,
        ingestion,
        ingestion_contracts,
        ingestion_readers,
    )

__all__ = [
    "apple_archive",
    "apple_import",
    "apple_normalize",
    "apple_readers",
    "credentials",
    "demo_archive",
    "demo_data",
    "discovery",
    "fit_decode",
    "fit_import",
    "fit_records",
    "garmin_activity_identity",
    "garmin_activity_state",
    "garmin_bounded",
    "garmin_checkpoints",
    "garmin_current",
    "garmin_daily_state",
    "garmin_health",
    "garmin_ingestion",
    "garmin_kinds",
    "garmin_membership",
    "garmin_normalization",
    "garmin_normalize",
    "garmin_normalize_types",
    "garmin_normalize_writing",
    "garmin_pull",
    "garmin_readers",
    "garmin_time",
    "garmin_versions",
    "ingestion",
    "ingestion_contracts",
    "ingestion_readers",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
