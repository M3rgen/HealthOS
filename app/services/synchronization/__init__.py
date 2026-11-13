"""Public synchronization service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        coverage,
        coverage_raw,
        coverage_reading,
        coverage_retrieval,
        coverage_types,
        pipeline_health,
        reconciliation_reports,
        retrieval,
        retrieval_coverage,
        scheduler,
        scheduler_shutdown,
        sync_contracts,
        sync_jobs,
        sync_reporting,
        sync_status,
        webhook_ingestion,
        webhook_metadata,
        webhook_reporting,
        webhook_status,
    )

__all__ = [
    "coverage",
    "coverage_raw",
    "coverage_reading",
    "coverage_retrieval",
    "coverage_types",
    "pipeline_health",
    "reconciliation_reports",
    "retrieval",
    "retrieval_coverage",
    "scheduler",
    "scheduler_shutdown",
    "sync_contracts",
    "sync_jobs",
    "sync_reporting",
    "sync_status",
    "webhook_ingestion",
    "webhook_metadata",
    "webhook_reporting",
    "webhook_status",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
