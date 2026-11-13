"""The workflow lint gate, its verified tool and summary result must remain blocking."""

from __future__ import annotations

import re

import pytest
from test_ci_gate_behaviour import FAIL, SUCCEED, run_step, step_of
from test_ci_gates import (
    Files,
    drop_step,
    drops_need,
    in_ci,
    load,
    rewrites_run,
    sets,
    step_named,
    step_setting,
    violations,
)

AMD64 = "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
ARM64 = "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6"
SHELLCHECK_X64 = "8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198"
SHELLCHECK_ARM64 = "12b331c1d2db6b9eb13cfca64306b1b157a86eb69db83023e261eaa7e7c14588"


def workflow_violations(files: Files) -> list[str]:
    found = violations(files)
    job = files.ci["jobs"].get("workflows", {"steps": []})
    install = next((s for s in job["steps"] if "actionlint.tar.gz" in s.get("run", "")), {})
    run = install.get("run", "")
    for required in (
        f"X64) asset=linux_amd64; digest={AMD64} ;;",
        f"ARM64) asset=linux_arm64; digest={ARM64} ;;",
        "https://github.com/rhysd/actionlint/releases/download/v1.7.12/",
        "actionlint_1.7.12_${asset}.tar.gz",
        "--output actionlint.tar.gz",
        'echo "$digest  actionlint.tar.gz" | sha256sum --check --strict',
        "--retry 5 --retry-all-errors",
        "tar -xzf actionlint.tar.gz -C actionlint-bin actionlint",
        'echo "$RUNNER_TEMP/actionlint-bin" >> "$GITHUB_PATH"',
    ):
        if required not in run:
            found.append(f"workflows: missing verified install requirement: {required}")
    if install.get("working-directory") != "${{ runner.temp }}":
        found.append("workflows: install must use runner.temp")
    # The local act image ships no ShellCheck; both environments install the same pinned build.
    shell = next((s for s in job["steps"] if "shellcheck.tar.xz" in s.get("run", "")), {})
    for required in (
        f"X64) asset=x86_64; digest={SHELLCHECK_X64} ;;",
        f"ARM64) asset=aarch64; digest={SHELLCHECK_ARM64} ;;",
        "https://github.com/koalaman/shellcheck/releases/download/v0.11.0/",
        "shellcheck-v0.11.0.linux.${asset}.tar.xz",
        'echo "$digest  shellcheck.tar.xz" | sha256sum --check --strict',
        "--retry 5 --retry-all-errors",
        'echo "$RUNNER_TEMP/shellcheck-bin" >> "$GITHUB_PATH"',
    ):
        if required not in shell.get("run", ""):
            found.append(f"workflows: missing verified ShellCheck requirement: {required}")
    if shell.get("working-directory") != "${{ runner.temp }}":
        found.append("workflows: ShellCheck install must use runner.temp")
    checkout = next((s for s in job["steps"] if "actions/checkout@" in s.get("uses", "")), {})
    if checkout.get("with", {}).get("persist-credentials") is not False:
        found.append("workflows: checkout persists credentials")
    summary = files.ci["jobs"]["summary"]["steps"][0]["run"]
    if not re.search(r'echo "\| İş akışları ve kabuk denetimi \| \$WORKFLOWS \|', summary):
        found.append("summary: missing workflow result row")
    return found


MUTANTS = {
    "job-removed": in_ci(lambda j: j.pop("workflows")),
    "job-tolerated": in_ci(sets("workflows", "continue-on-error", True)),
    "step-removed": in_ci(lambda j: drop_step(j["workflows"], "actionlint -color")),
    "step-tolerated": in_ci(
        step_setting("workflows", "actionlint -color", "continue-on-error", True)
    ),
    "step-disabled": in_ci(step_setting("workflows", "actionlint -color", "if", "${{ false }}")),
    "summary-need-removed": in_ci(drops_need("workflows")),
    "summary-result-removed": in_ci(lambda j: j["summary"]["steps"][0]["env"].pop("WORKFLOWS")),
    "summary-loop-removed": in_ci(rewrites_run("summary", "for result", ' "$WORKFLOWS"', "")),
    "summary-row-removed": in_ci(rewrites_run("summary", "for result", "| $WORKFLOWS |", "| — |")),
    "amd64-digest-changed": in_ci(rewrites_run("workflows", "curl", AMD64, "0" * 64)),
    "arm64-digest-changed": in_ci(rewrites_run("workflows", "curl", ARM64, "0" * 64)),
    "checksum-unchecked": in_ci(
        rewrites_run("workflows", "curl", "sha256sum --check --strict", ":")
    ),
    "download-no-retry": in_ci(rewrites_run("workflows", "curl", "--retry-all-errors", "")),
    "tool-version-changed": in_ci(rewrites_run("workflows", "curl", "v1.7.12/", "latest/")),
    "shellcheck-install-removed": in_ci(lambda j: drop_step(j["workflows"], "shellcheck.tar.xz")),
    "shellcheck-digest-changed": in_ci(
        rewrites_run("workflows", "shellcheck.tar.xz", SHELLCHECK_ARM64, "0" * 64)
    ),
    "shellcheck-unchecked": in_ci(
        rewrites_run("workflows", "shellcheck.tar.xz", "sha256sum --check --strict", ":")
    ),
    "shellcheck-disabled": in_ci(
        rewrites_run("workflows", "actionlint -color", "-color", "-shellcheck=")
    ),
    "findings-ignored": in_ci(
        rewrites_run("workflows", "actionlint -color", "-color", "-color -ignore .")
    ),
    "checkout-credentials": in_ci(
        lambda j: step_named(j["workflows"], "actions/checkout@")["with"].update(
            {"persist-credentials": True}
        )
    ),
}


def test_workflow_gate_policy():
    assert workflow_violations(load()) == []


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_weakened_workflow_gate_is_reported(name):
    assert workflow_violations(MUTANTS[name](load())), f"{name} was not reported"


def test_lint_step_propagates_tool_failure(tmp_path):
    result = run_step(
        step_of("ci.yml", "workflows", "actionlint -color"),
        tmp_path,
        {"actionlint": FAIL, "shellcheck": SUCCEED},
    )
    assert result.returncode == 3


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped"])
def test_summary_requires_successful_workflow_lint(tmp_path, outcome):
    step = load().ci["jobs"]["summary"]["steps"][0]
    result = run_step(
        step,
        tmp_path,
        **{**dict.fromkeys(step["env"], "success"), "WORKFLOWS": outcome},
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"),
    )
    assert result.returncode == (0 if outcome == "success" else 1)
