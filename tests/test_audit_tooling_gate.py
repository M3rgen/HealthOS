"""The scheduled audit fails on build-tooling advisories instead of warning."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "audit.yml"


def tooling_step() -> dict[str, object]:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["audit"]["steps"]
    (step,) = [item for item in steps if item.get("id") == "npm_tooling"]
    return step


def test_build_tooling_audit_is_a_gate() -> None:
    step = tooling_step()
    assert step["run"].strip() == "npm audit --audit-level=high"
    assert step.get("continue-on-error") in (None, False)
    assert "::warning::npm audit reports build-tooling advisories" not in WORKFLOW.read_text()


def test_audit_summary_reports_the_tooling_step() -> None:
    assert "steps.npm_tooling.outcome" in WORKFLOW.read_text(encoding="utf-8")
