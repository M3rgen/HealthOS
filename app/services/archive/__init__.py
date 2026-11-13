"""Public archive service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        archive_verification,
        manual_flags,
        normalization,
        raw_history,
        raw_history_import,
        raw_history_source,
        raw_history_timeline,
        raw_history_types,
        raw_history_verification,
        raw_history_writing,
        raw_rejections,
        responses,
        source_deletions,
        storage,
        storage_context,
    )

__all__ = [
    "archive_verification",
    "manual_flags",
    "normalization",
    "raw_history",
    "raw_history_import",
    "raw_history_source",
    "raw_history_timeline",
    "raw_history_types",
    "raw_history_verification",
    "raw_history_writing",
    "raw_rejections",
    "responses",
    "source_deletions",
    "storage",
    "storage_context",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
