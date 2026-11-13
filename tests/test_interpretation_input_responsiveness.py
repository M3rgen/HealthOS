"""Large synthetic input CPU work leaves the loop and session thread available."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from derived_records import DAY
from support.input_latency import raw_rows, synthetic_session

from app.services.interpretation import interpretation_inputs as inputs
from app.services.shared.query_ranges import CivilDateRange

# The batch size of interpretation_inputs._snapshot_fields and input_rows: at most one batch
# of rows may be handled on the session's thread between two turns of the event loop.
BATCH = 256
TOTAL_ROWS = 44_000
# A row's JSON is a few hundred characters; encoding all 44,000 at once would be millions.
ONE_ROW_ENCODING_LIMIT = 16_384


def run_isolated(sample):
    """A sample in a fresh process: it patches the module and leaves nothing to later tests."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, json, sys; sys.path.insert(0, 'tests'); "
            f"from support.input_latency import {sample}; "
            f"print(json.dumps(asyncio.run({sample}())))",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_44000_inputs_yield_to_the_loop_between_batches_and_never_send_session_or_orm_to_thread():
    # The sample asserts the thread rules, the loaded rows and that no transaction was ended.
    # Counts, not seconds: a host under load runs the same batches between the same yields.
    report = run_isolated("synthetic_sample")
    assert report["worst_rows_between_turns"] == BATCH, report
    assert report["turns"] >= TOTAL_ROWS // BATCH, report
    assert report["workers"] == 2
    # Worker threads share the GIL, so one huge json.dumps would stall the loop just the same.
    assert report["largest_encoding"] < ONE_ROW_ENCODING_LIMIT, report
    assert len(report["fingerprint"]) == 64


async def test_worker_canonical_utf8_budget_rejection_propagates(monkeypatch):
    rows = raw_rows(1)
    session, _ = synthetic_session(rows)
    payload = rows[0].raw_json
    byte_size = len(inputs.canonical_json(payload).encode())
    monkeypatch.setattr(inputs, "MAX_INPUT_BYTES", byte_size - 1)
    with pytest.raises(inputs.BudgetError, match="input_budget_exceeded"):
        await inputs.load_inputs(session, {"steps"}, dates=CivilDateRange(DAY, DAY, "UTC"))
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


def test_44000_database_rows_hydrate_in_batches_between_loop_yields():
    report = run_isolated("database_sample")
    assert report["rows"] == TOTAL_ROWS and len(report["fingerprint"]) == 64
    assert report["worst_rows_between_turns"] <= BATCH, report
    assert report["turns"] >= TOTAL_ROWS // BATCH, report


@pytest.mark.parametrize("count", [0, 1, 44_000])
def test_streamed_signature_preserves_canonical_hash_bytes(count):
    signature = {
        "raw": [
            [str(i), {"Türkçe": "ölçüm", "escape": '\\"'}, None, False, 1.25] for i in range(count)
        ],
        "manual": [],
        "workouts": [],
        "flags": [["1", "mark"], ["2", "unmark"]],
        "devices": [["steps", "synthetic", "cihaz", 1]],
    }
    assert inputs._signature_hash(
        {key: iter(rows) for key, rows in signature.items()}
    ) == inputs.payload_hash(signature)
