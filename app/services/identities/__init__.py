"""Public identities service modules, loaded only when requested."""

from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import (
        analytics_views,
        deferred_deletions,
        hollow_names,
        identity,
        identity_backfill,
        identity_backfill_agreement,
        identity_backfill_chains,
        identity_backfill_linking,
        identity_backfill_planning,
        identity_backfill_reading,
        identity_backfill_reporting,
        identity_backfill_state,
        identity_backfill_stats,
        identity_common,
        identity_decisions,
        identity_options,
        identity_presence,
        identity_reconcile_window,
        identity_reconciler,
        identity_resolution,
        identity_schema,
        identity_verification,
        identity_verification_links,
        identity_writes,
        source_chain_evidence,
        source_chain_locks,
        source_chain_model,
        source_chain_repository,
        source_chain_rules,
        source_chain_state,
        source_chains,
        source_precedence,
    )

__all__ = [
    "analytics_views",
    "deferred_deletions",
    "hollow_names",
    "identity",
    "identity_backfill",
    "identity_backfill_agreement",
    "identity_backfill_chains",
    "identity_backfill_linking",
    "identity_backfill_planning",
    "identity_backfill_reading",
    "identity_backfill_reporting",
    "identity_backfill_state",
    "identity_backfill_stats",
    "identity_common",
    "identity_decisions",
    "identity_options",
    "identity_presence",
    "identity_reconcile_window",
    "identity_reconciler",
    "identity_resolution",
    "identity_schema",
    "identity_verification",
    "identity_verification_links",
    "identity_writes",
    "source_chain_evidence",
    "source_chain_locks",
    "source_chain_model",
    "source_chain_repository",
    "source_chain_rules",
    "source_chain_state",
    "source_chains",
    "source_precedence",
]


def __getattr__(name: str) -> ModuleType:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module
