"""Validate CI YAML and execute its result aggregation script."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_ci_yaml_and_immutable_actions():
    for path in [*WORKFLOWS.glob("*.yml"), ROOT / ".github/dependabot.yml"]:
        document = yaml.safe_load(path.read_text())
        for job in document.get("jobs", {}).values():
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w./-]+@[a-f0-9]{40}", step["uses"])


@pytest.mark.parametrize(
    "gate,outcome",
    [(gate, "failure") for gate in ("VERIFY", "INSTALLER", "BROWSER", "SECURITY", "SECRETS")]
    + [("SECURITY", outcome) for outcome in ("success", "cancelled", "skipped")],
)
def test_summary_reports_results_and_propagates_gate_failure(tmp_path, gate, outcome):
    summary = tmp_path / "summary.md"
    job = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]["summary"]
    environment = {
        **os.environ,
        **dict.fromkeys(job["steps"][0]["env"], "success"),
        gate: outcome,
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", job["steps"][0]["run"]],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == (0 if outcome == "success" else 1), result.stderr
    table = summary.read_text()
    assert f"| {outcome} |" in table
    assert "Coverage" in table and "Henüz ölçülmüyor" not in table
