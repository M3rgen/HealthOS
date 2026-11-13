"""Retained parallel-lane exports and direct production callers of the split modules."""

import ast
import importlib
from pathlib import Path

import pytest

import app.services.identities.source_chain_model as source_chain_model

ROOT = Path(__file__).resolve().parents[1]

# Only the exports used by the protected parallel lane remain in these facades.
SURFACE = [
    ("app.models.identity", "app.models.identity_queries", "in_force plan_each_query"),
    (
        "app.models.identity",
        "app.models.identity_tables",
        "IdentityDeferredDeletion RawHealthDataSupersession",
    ),
    ("app.services.identities.identity", "app.services.identities.identity_common", "among"),
    (
        "app.services.identities.identity",
        "app.services.identities.identity_presence",
        "present_records",
    ),
    (
        "app.services.identities.source_chains",
        "app.services.identities.source_chain_locks",
        "lock_data_type try_lock_deferrals",
    ),
    (
        "app.services.identities.source_chains",
        "app.services.identities.source_chain_model",
        "Chains",
    ),
    (
        "app.services.identities.source_chains",
        "app.services.identities.source_chain_repository",
        "load_chains",
    ),
]


@pytest.mark.parametrize("facade,implementation,names", SURFACE)
def test_remaining_exports_are_the_implementing_objects(facade, implementation, names):
    exported = importlib.import_module(facade)
    implemented = importlib.import_module(implementation)
    for name in names.split():
        assert getattr(exported, name) is getattr(implemented, name)


def test_removed_facade_exports_do_not_return():
    expected = {}
    for facade, _, names in SURFACE:
        expected.setdefault(facade, set()).update(names.split())
    for facade in (
        *expected,
        "app.services.archive.raw_history",
        "app.services.identities.identity_backfill",
    ):
        path = ROOT / (facade.replace(".", "/") + ".py")
        aliases = {
            alias.name
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == alias.asname
        }
        assert aliases == expected.get(facade, set()), facade


def test_production_callers_use_the_extracted_model_rules_and_repository():
    from app.services.archive import source_deletions as deletions
    from app.services.archive import storage
    from app.services.identities import identity, identity_backfill_chains, identity_decisions
    from app.services.identities import source_chain_model as model
    from app.services.identities import source_chain_repository as repository
    from app.services.identities import source_chain_rules as rules

    assert (
        source_chain_model.Chains is identity_decisions.Chains is deletions.Chains is model.Chains
    )
    assert source_chain_model.SourceState is model.SourceState
    assert identity_backfill_chains.decide_chains is rules.decide_chains
    assert storage.record_sightings is repository.record_sightings
    assert (
        storage.record_evidence
        is identity_backfill_chains.record_evidence
        is repository.record_evidence
    )
    assert identity.load_state is repository.load_state
    assert deletions.load_chains is identity_backfill_chains.load_chains is repository.load_chains
    assert identity_decisions.record_decision is repository.record_decision
    for name in ("insert_chain", "record_devices", "record_refutations", "write_spans"):
        assert getattr(identity_backfill_chains, name) is getattr(repository, name)


def test_explicit_reexports_do_not_grow_elsewhere():
    count = sum(
        alias.name == alias.asname
        for path in (ROOT / "app").rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    assert count <= 98, f"Explicit re-exports grew beyond the reduced surface: {count}"
