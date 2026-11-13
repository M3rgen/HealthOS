"""Size limits and value-object boundaries for the identity function refactor."""

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path

import pytest
from identity_records import HEART, T0

from app.core.timeutil import as_utc
from app.models.identity_tables import IdentityReconcileState
from app.services.identities.identity_backfill_state import ReconcileResult, save_state
from app.services.identities.identity_backfill_stats import BackfillStats
from app.services.identities.source_chain_model import Chains
from app.services.identities.source_chain_state import ChainHistory

MODULES = (
    "identity",
    "identity_backfill",
    "identity_backfill_chains",
    "identity_backfill_linking",
    "identity_backfill_reading",
    "identity_backfill_reporting",
    "identity_backfill_state",
    "source_chain_state",
    "source_chain_model",
)
ROOT = Path(__file__).resolve().parents[1] / "app/services/identities"


@pytest.mark.parametrize("module", MODULES)
def test_identity_functions_and_helpers_fit_the_issue_budgets(module):
    for node in ast.walk(ast.parse((ROOT / f"{module}.py").read_text())):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        assert node.end_lineno - node.lineno + 1 <= 80, node.name
        args = node.args
        parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        count = sum(arg.arg not in {"self", "cls"} for arg in parameters)
        count += int(args.vararg is not None) + int(args.kwarg is not None)
        assert count <= 6, node.name


def test_grouped_chain_history_and_copies_own_their_recorded_state():
    pair = ("a", "b")
    decisions = {pair: "confirm"}
    devices = {"a": "a", "b": "a"}
    history = ChainHistory(
        chain_decisions=decisions,
        pair_decisions={pair: "not-distinct"},
        refuted_events={pair},
        logged_devices=devices,
        agreement={pair: (3, 1, True)},
        premises={pair: frozenset({("earlier", "later")})},
    )
    with pytest.raises(FrozenInstanceError):
        history.refuted_events = ()
    original = Chains(links={pair}, decided_by={pair: "owner"}, history=history)
    decisions.clear()
    devices.clear()
    assert original.live(*pair) and original.same_device(*pair)
    copied = original.copy()
    for field in (
        "links",
        "decided_by",
        "chain_decisions",
        "pair_decisions",
        "refuted_events",
        "logged_devices",
        "agreement",
        "premises",
    ):
        assert getattr(copied, field) == getattr(original, field)
    copied.decide("reject", *pair)
    copied.log_devices({"b": "b"})
    copied.update_agreement({pair: (0, 4, True)})
    copied.set_premise(*pair, frozenset())
    assert not copied.live(*pair) and not copied.same_device(*pair)
    assert original.live(*pair) and original.same_device(*pair)
    assert original.logged_devices == {"a": "a", "b": "a"}
    assert original.agreement == {pair: (3, 1, True)}
    assert original.premises == {pair: frozenset({("earlier", "later")})}


@pytest.mark.parametrize(
    ("mode", "checkpoint", "skipped", "error", "advances"),
    [
        ("full", True, None, None, True),
        ("incremental", True, None, None, True),
        ("decision", False, None, None, False),
        ("full", True, "already processed", None, False),
        ("incremental", True, None, "failure" * 400, False),
    ],
)
async def test_grouped_run_result_preserves_checkpoints_and_failed_run_history(
    sessions, clock, mode, checkpoint, skipped, error, advances
):
    initial = BackfillStats(mode="full", rows=3)
    await save_state(sessions, HEART.name, ReconcileResult(T0, initial))
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, HEART.name)
        full_run_at = as_utc(state.full_run_at)
    started = T0 + timedelta(hours=1)
    clock.advance(hours=2)
    stats = None if error else BackfillStats(mode=mode, rows=7, skipped=skipped)
    result = ReconcileResult(started, stats, error=error, checkpoint=checkpoint, mode=mode)
    with pytest.raises(FrozenInstanceError):
        result.checkpoint = False
    await save_state(sessions, HEART.name, result)
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, HEART.name)
        assert state.runs == 2 and state.last_mode == mode
        assert as_utc(state.last_started_at) == started
        assert as_utc(state.last_finished_at) == clock.now
        assert as_utc(state.updated_at) == clock.now
        assert as_utc(state.checkpoint_at) == (started if advances else T0)
        assert as_utc(state.full_run_at) == (
            clock.now if advances and mode == "full" else full_run_at
        )
        assert state.last_error == (error[:2000] if error else None)
        assert json.loads(state.last_result) == (stats or initial).summary()
