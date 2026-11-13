"""The installer CI jobs (IP-7/6, IP-12/3): linted scripts, Node 24 actions pinned by SHA with
their version, and runner images pinned on purpose instead of *-latest."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
INSTALLER_JOBS = (
    "installer_lint",
    "installer",
    "installer_hosts",
    "installer_linux_docker",
    "installer_windows_ps51",
)
# Versions whose action.yml declares `using: node24` (checked when the pins were chosen).
NODE24 = {"actions/checkout": (7, 0, 1), "astral-sh/setup-uv": (10, 3, 3)}


def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_installer_jobs_pin_their_runner_images():
    for name in INSTALLER_JOBS:
        job = jobs()[name]
        runners = job.get("strategy", {}).get("matrix", {}).get("os") or [job["runs-on"]]
        for runner in runners:
            assert re.fullmatch(r"(ubuntu|macos|windows)-\d+(\.\d+)?", runner), (name, runner)


def test_installer_jobs_use_node24_actions_pinned_by_sha():
    for name in INSTALLER_JOBS:
        for step in jobs()[name]["steps"]:
            if "uses" in step:
                action, _, sha = step["uses"].partition("@")
                assert re.fullmatch(r"[0-9a-f]{40}", sha), step["uses"]
                assert action in NODE24, action
    # In the installer section every pin names its exact Node 24 version (the jobs above it
    # belong to main's CI).
    section = WORKFLOW.read_text(encoding="utf-8").split("\n  installer_lint:\n", 1)[1]
    for line in section.splitlines():
        if "uses:" in line:
            action = line.split("uses:", 1)[1].strip().split("@", 1)[0]
            tag = "v" + ".".join(map(str, NODE24[action]))
            assert line.endswith(f" # {tag}"), line


def test_lint_job_runs_shellcheck_and_psscriptanalyzer():
    steps = " ".join(step.get("run", "") for step in jobs()["installer_lint"]["steps"])
    assert "shellcheck install.sh\n" in steps
    assert "shellcheck --severity=warning scripts/*.sh" in steps
    assert "Invoke-ScriptAnalyzer" in steps and "PSScriptAnalyzerSettings.psd1" in steps
    assert "throw" in steps  # findings fail the job
    assert (ROOT / ".github" / "PSScriptAnalyzerSettings.psd1").is_file()


def test_docker_install_job_covers_full_mode_without_google():
    include = jobs()["installer_linux_docker"]["strategy"]["matrix"]["include"]
    modes = {(entry["mode"], entry["sources"]) for entry in include}
    assert ("trial", "none") in modes
    assert any(mode == "full" and "google" not in sources for mode, sources in modes)
