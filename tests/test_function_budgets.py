"""Bound production function size, including nested functions and operating scripts."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_production_functions_stay_within_length_budgets():
    # Historical migrations retain their original schema operations. Tests are not
    # runtime code; count the application and the scripts used to operate it.
    lengths = {
        f"{path.relative_to(ROOT)}:{node.lineno} {node.name}": node.end_lineno - node.lineno + 1
        for directory in ("app", "scripts")
        for path in (ROOT / directory).rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert not (oversized := {name: size for name, size in lengths.items() if size > 150}), (
        oversized
    )
    long = {name: size for name, size in lengths.items() if size > 100}
    assert len(long) <= 2, long


@pytest.mark.parametrize(
    "module",
    [
        "app/services/archive/storage.py",
        "app/services/archive/source_deletions.py",
        "app/services/interpretation/interpretation_inputs.py",
        "app/services/umay/codex_health.py",
        "app/sync/incremental.py",
    ],
)
def test_refactored_functions_and_their_parts_stay_within_eighty_lines(module):
    tree = ast.parse((ROOT / module).read_text())
    oversized = {
        node.name: node.end_lineno - node.lineno + 1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.end_lineno - node.lineno + 1 > 80
    }
    assert not oversized, oversized


@pytest.mark.parametrize("complexity, rejected", [(16, False), (17, True)])
def test_ruff_enforces_complexity_at_the_configured_boundary(complexity, rejected):
    source = "def probe(value):\n" + "".join(
        f"    if value == {number}:\n        return {number}\n" for number in range(complexity - 1)
    )
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--stdin-filename", "app/probe.py", "-"],
        input=source,
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == int(rejected), result.stdout + result.stderr
    assert ("C901" in result.stdout) == rejected


def test_round_five_boundary_functions_have_complexity_headroom():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=15",
            "app/main.py",
            "app/services/identities/identity_backfill_chains.py",
            "app/services/identities/identity_decisions.py",
            "app/services/interpretation/sleep_timeline.py",
            "app/services/synchronization/coverage_reading.py",
            "app/services/umay/health_transfer.py",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
