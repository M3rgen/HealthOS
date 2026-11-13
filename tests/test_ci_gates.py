"""CI, release and repository-protection gates cannot be removed or switched off unnoticed.

``violations`` is the policy; the real files must satisfy it, and each mutant below, alone or
together with the others, must be reported by it.
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml
from support.ci_gate_commands import INSTALLER_GATES, REQUIRED_COMMANDS

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
GATES = (
    "verify",
    "bridge_boundary",
    "installer",
    "browser",
    "e2e",
    "security",
    "workflows",
    "secrets",
    "codeql",
    "smoke",
) + INSTALLER_GATES
# The only jobs that may skip their real work under `act`, each with a step saying so.
ACT_SKIPPING_GATES = ("codeql", "smoke", *INSTALLER_GATES)
UV_RUN = "uv run --frozen --extra dev "
# The blocking Python: an exact patch matching the shipped image.
CI_PYTHON = "3.12.16"
# From gitleaks_8.30.1_checksums.txt of the upstream release (linux_x64, what ubuntu-24.04 runs).
GITLEAKS_X64_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
# No role may bypass repository protection; publishing requires environment review.
EXPECTED_BYPASS = []
REQUIRED_CONTEXTS = {"summary", "CodeQL (python)", "CodeQL (javascript-typescript)"}

AUDIT_COMMANDS = (
    r"uvx .*pip-audit==\d+\.\d+\.\d+ -r /dev/stdin --strict$",
    r"npm audit --omit=dev --audit-level=high$",
    r"docker build --tag [\w.-]+:audit \.$",
)
FORBIDDEN_IN_RUN = re.compile(
    r"--co\b|--collect-only|--deselect|--ignore\b|--ignore-glob|--exit-zero|--no-cov\b"
    r"|--fail-under=0\b|--cov-fail-under=0\b|(^|\s)-k\s|\|\|\s*(true|:|exit 0)\b"
    r"|^\s*exit 0\b|\bset \+e\b"
)
ALLOWED_STEP_CONDITIONS = {
    "${{ !cancelled() }}",
    "${{ !cancelled() && steps.image.outcome == 'success' }}",
}
ACT_CONDITIONS = {"${{ !env.ACT }}", "${{ env.ACT }}"}


@dataclass(frozen=True)
class Files:
    ci: dict
    release: dict
    audit: dict
    clock: dict
    codeql: dict
    main_ruleset: dict
    tag_ruleset: dict
    codeowners: str
    makefile: str
    pyproject: str
    ci_gate: str
    ci_text: str
    dockerfile: str
    clock_matrix: str


def load() -> Files:
    return Files(
        ci=yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8")),
        release=yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8")),
        audit=yaml.safe_load((WORKFLOWS / "audit.yml").read_text(encoding="utf-8")),
        clock=yaml.safe_load((WORKFLOWS / "clock.yml").read_text(encoding="utf-8")),
        codeql=yaml.safe_load((WORKFLOWS / "codeql.yml").read_text(encoding="utf-8")),
        main_ruleset=json.loads((ROOT / ".github/rulesets/main.json").read_text(encoding="utf-8")),
        tag_ruleset=json.loads(
            (ROOT / ".github/rulesets/release-tags.json").read_text(encoding="utf-8")
        ),
        codeowners=(ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8"),
        makefile=(ROOT / "Makefile").read_text(encoding="utf-8"),
        pyproject=(ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        ci_gate=(ROOT / "scripts/ci_gate.sh").read_text(encoding="utf-8"),
        ci_text=(WORKFLOWS / "ci.yml").read_text(encoding="utf-8"),
        dockerfile=(ROOT / "Dockerfile").read_text(encoding="utf-8"),
        clock_matrix=(ROOT / "tests/support/clock_matrix.sh").read_text(encoding="utf-8"),
    )


def shell_commands(run: str) -> list[str]:
    """Executable commands of a ``run`` block: comments dropped, split on pipes and ``&&``."""
    joined = re.sub(r"\\\n\s*", " ", run)
    commands = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in re.split(r"\s\|\s|\s&&\s", line):
            commands.append(part.strip().removeprefix(UV_RUN))
    return commands


def step_violations(job_name: str, job: dict) -> list[str]:
    found = []
    commands: list[str] = []
    for step in job["steps"]:
        label = f"{job_name}: step {step.get('name', step.get('uses'))}"
        if step.get("continue-on-error"):
            found.append(f"{label} has continue-on-error")
        allowed = ALLOWED_STEP_CONDITIONS | (
            ACT_CONDITIONS if job_name in ACT_SKIPPING_GATES else set()
        )
        if "if" in step and step["if"] not in allowed:
            found.append(f"{label} has condition {step['if']!r}")
        run = step.get("run", "")
        step_commands = shell_commands(run)
        commands.extend(step_commands)
        for command in step_commands:
            if FORBIDDEN_IN_RUN.search(command):
                found.append(f"{label} weakens a gate: {command}")
    for pattern in REQUIRED_COMMANDS[job_name]:
        if not any(re.match(pattern, command) for command in commands):
            found.append(f"{job_name}: required command missing: {pattern}")
    return found


def trivy_violations(where: str, job: dict) -> list[str]:
    steps = [s for s in job["steps"] if "aquasecurity/trivy-action" in s.get("uses", "")]
    if len(steps) != 1:
        return [f"{where}: expected one Trivy step, found {len(steps)}"]
    settings = steps[0]["with"]
    found = []
    if str(settings.get("exit-code")) != "1":
        found.append(f"{where}: Trivy does not fail the job")
    if "CRITICAL" not in settings.get("severity", "") or "HIGH" not in settings.get("severity", ""):
        found.append(f"{where}: Trivy severity narrowed")
    return found


def ci_violations(ci: dict) -> list[str]:
    found = []
    triggers = ci["on"]
    push = triggers.get("push") or {}
    if push.get("branches") != ["main"] or "pull_request" not in triggers:
        found.append("ci.yml must run on push to main and on pull requests")
    if {"paths", "paths-ignore", "branches-ignore"} & set(push):
        found.append("ci.yml push trigger is filtered")
    concurrency = ci.get("concurrency", {})
    if concurrency.get("cancel-in-progress") != "${{ github.ref != 'refs/heads/main' }}":
        found.append("ci.yml may cancel a run on main")
    if "github.sha" not in concurrency.get("group", ""):
        found.append("ci.yml main runs share a concurrency group and can replace each other")
    jobs = ci["jobs"]
    for name in GATES:
        if name not in jobs:
            found.append(f"gate job {name} is missing")
            continue
        job = jobs[name]
        if "if" in job:
            found.append(f"gate job {name} has condition {job['if']!r}")
        allowed = "${{ matrix.experimental }}" if name == "verify" else None
        if job.get("continue-on-error") not in (None, allowed):
            found.append(f"gate job {name} has continue-on-error")
        found.extend(step_violations(name, job))
    if "verify" in jobs:
        entries = jobs["verify"]["strategy"]["matrix"]["include"]
        if not any(e["python"] == CI_PYTHON and e["experimental"] is False for e in entries):
            found.append(f"verify: Python {CI_PYTHON} is not a blocking matrix entry")
        found.extend(combined_coverage_violations(jobs["verify"]))
    found.extend(act_skip_violations(ci))
    found.extend(smoke_manifest_violations(ci))
    found.extend(codeql_gate_violations(jobs.get("codeql")))
    found.extend(gitleaks_violations(jobs.get("secrets")))
    if "security" in jobs:
        found.extend(trivy_violations("ci.yml security", jobs["security"]))
    found.extend(summary_violations(jobs.get("summary")))
    return found


def combined_coverage_violations(verify: dict) -> list[str]:
    found = []
    runs = [step.get("run", "") for step in verify["steps"]]
    required = (
        "make coverage-security\nmkdir -p .test-tmp-ci-coverage\n"
        "cp .coverage .test-tmp-ci-coverage/.coverage.sqlite\n",
        "make test-postgres\ncp .coverage .test-tmp-ci-coverage/.coverage.postgres\n",
        "make ci-coverage-report",
    )
    if not all(run in runs for run in required):
        return ["verify: coverage collection or combined report is missing"]
    if [runs.index(run) for run in required] != sorted(runs.index(run) for run in required):
        found.append("verify: combined report must follow SQLite and PostgreSQL")
    postgres = verify["steps"][runs.index(required[1])]
    if postgres.get("env", {}).get("PYTEST_ADDOPTS") != (
        "--cov=app --cov-append --cov-report=term"
    ):
        found.append("verify: both PostgreSQL suites must append app coverage")
    return found


def act_skip_violations(ci: dict) -> list[str]:
    """Only a real run may skip under act, and nothing may pretend to be act on GitHub."""
    found = []
    environments = [ci.get("env", {})]
    for job in ci["jobs"].values():
        environments.append(job.get("env", {}))
        environments.extend(step.get("env", {}) for step in job["steps"])
    if any("ACT" in env for env in environments):
        found.append("ci.yml sets ACT, which would skip CodeQL and the smoke test on GitHub")
    for name in ACT_SKIPPING_GATES:
        job = ci["jobs"].get(name)
        if job is None:
            continue
        real = [step for step in job["steps"] if step.get("if") == "${{ !env.ACT }}"]
        notices = [step for step in job["steps"] if step.get("if") == "${{ env.ACT }}"]
        if not real or len(notices) != 1 or "::warning::" not in notices[0].get("run", ""):
            found.append(f"{name}: an act skip must announce itself with one warning step")
    return found


def codeql_gate_violations(job: dict | None) -> list[str]:
    if job is None:
        return []
    found = []
    if job["strategy"]["matrix"].get("language") != ["python", "javascript-typescript"]:
        found.append("codeql: a language was dropped")
    uses = [step.get("uses", "") for step in job["steps"]]
    for action in ("github/codeql-action/init@", "github/codeql-action/analyze@"):
        if not any(action in use for use in uses):
            found.append(f"codeql: {action} is missing")
    return found


def gitleaks_violations(job: dict | None) -> list[str]:
    if job is None:
        return []
    install = next((s["run"] for s in job["steps"] if "gitleaks.tar.gz" in s.get("run", "")), "")
    found = []
    # A shallow clone leaves `gitleaks git` nothing but the tip commit to read.
    checkout = next(s for s in job["steps"] if "actions/checkout@" in s.get("uses", ""))
    if checkout.get("with", {}).get("fetch-depth") != 0:
        found.append("secrets: the history scan does not fetch the whole history")
    if f"X64) asset=linux_x64; digest={GITLEAKS_X64_SHA256} ;;" not in install:
        found.append("secrets: the linux_x64 Gitleaks digest changed")
    if "sha256sum --check --strict" not in install:
        found.append("secrets: the Gitleaks download is not verified")
    if "--retry 5" not in install or "--retry-all-errors" not in install:
        found.append("secrets: the Gitleaks download does not retry")
    return found


def python_pin_violations(files: Files) -> list[str]:
    found = []
    if len(re.findall(r"uv python install 3\.12\.16$", files.ci_text, re.MULTILINE)) < 3:
        found.append("ci.yml installs a floating Python 3.12")
    if re.search(r"uv python install 3\.12$|--python 3\.12 ", files.ci_text, re.MULTILINE):
        found.append("ci.yml installs a floating Python 3.12")
    if f"--matrix python:{CI_PYTHON} " not in files.ci_gate:
        found.append("make ci-gate does not select the blocking Python entry")
    patches = set(re.findall(r"^FROM python:(3\.\d+\.\d+)-", files.dockerfile, re.MULTILINE))
    if patches != {CI_PYTHON}:
        found.append(f"the image Python patch {patches} differs from CI {CI_PYTHON}")
    return found


def schedule_violations(files: Files) -> list[str]:
    found = []
    for name, workflow in (
        ("audit", files.audit),
        ("clock", files.clock),
        ("codeql", files.codeql),
    ):
        if not workflow["on"].get("schedule"):
            found.append(f"{name}.yml has no schedule")
    if {"push", "pull_request"} & set(files.codeql["on"]):
        found.append("codeql.yml duplicates the blocking CodeQL job of ci.yml")
    audit = files.audit["jobs"]["audit"]
    commands = [c for step in audit["steps"] for c in shell_commands(step.get("run", ""))]
    for pattern in AUDIT_COMMANDS:
        if not any(re.match(pattern, command) for command in commands):
            found.append(f"audit.yml: required command missing: {pattern}")
    if audit.get("continue-on-error") or "if" in audit:
        found.append("audit.yml can be bypassed")
    for step in audit["steps"]:
        if step.get("continue-on-error"):
            found.append("audit.yml: a finding would not fail the run")
    found.extend(trivy_violations("audit.yml", audit))
    clock = files.clock["jobs"]["clock"]
    commands = [c for step in clock["steps"] for c in shell_commands(step.get("run", ""))]
    if "make test-clock" not in commands or clock.get("continue-on-error"):
        found.append("clock.yml does not run make test-clock as a failing job")
    return found


def summary_violations(summary: dict | None) -> list[str]:
    if summary is None:
        return ["summary job is missing"]
    found = []
    if set(summary.get("needs", [])) != set(GATES):
        found.append(f"summary needs {summary.get('needs')}, expected {GATES}")
    if summary.get("if") != "${{ always() }}":
        found.append("summary must run even when a gate failed")
    if summary.get("continue-on-error"):
        found.append("summary has continue-on-error")
    step = summary["steps"][0]
    if "continue-on-error" in step or "if" in step:
        found.append("summary step can be bypassed")
    for gate in GATES:
        variable = gate.upper()
        if step.get("env", {}).get(variable) != f"${{{{ needs.{gate}.result }}}}":
            found.append(f"summary does not read the result of {gate}")
        loop = re.search(r"for result in ([^;]*);", step["run"])
        if loop is None or f'"${variable}"' not in loop.group(1):
            found.append(f"summary does not test the result of {gate}")
    if 'test "$result" = success || exit 1' not in step["run"]:
        found.append("summary does not fail on a failed gate")
    return found


def release_violations(release: dict) -> list[str]:
    found = []
    tags = (release["on"].get("push") or {}).get("tags")
    if tags != ["v[0-9]+.[0-9]+.[0-9]+", "v[0-9]+.[0-9]+.[0-9]+.[0-9]+"]:
        found.append(f"release fires on tags {tags}")
    if release.get("permissions") != {}:
        found.append("release workflow grants default token permissions")
    jobs = release["jobs"]
    order = {
        "build": "gate",
        "attest": "build",
        "verify-provenance": "attest",
        "publish": "verify-provenance",
    }
    for job, dependency in order.items():
        if job not in jobs or jobs[job].get("needs") != dependency:
            found.append(f"release {job} does not depend on {dependency}")
    for name, job in jobs.items():
        if job.get("continue-on-error") or "if" in job:
            found.append(f"release job {name} can be bypassed")
        for step in job["steps"]:
            if step.get("continue-on-error") or "if" in step:
                found.append(f"release job {name}: step can be bypassed")
    if "gate" in jobs:
        commands = [c for s in jobs["gate"]["steps"] for c in shell_commands(s.get("run", ""))]
        for required in ("preflight --tag", "ci --commit"):
            if not any(
                required in command and "release_gate.py" in command for command in commands
            ):
                found.append(f"release gate does not run release_gate.py {required}")
        if jobs["gate"].get("permissions") != {"contents": "read", "actions": "read"}:
            found.append("release gate permissions changed")
    if "build" in jobs:
        found.extend(trivy_violations("release.yml build", jobs["build"]))
        found.extend(release_image_violations(jobs["build"]))
    if "publish" in jobs and jobs["publish"].get("environment") != "release":
        found.append("release publish has no `environment: release` approval")
    found.extend(release_permissions_violations(jobs))
    found.extend(release_provenance_violations(jobs))
    return found


def release_image_violations(build: dict) -> list[str]:
    """The release image must be compared with the image CI smoke-tested, before it is kept."""
    names = [step.get("name", step.get("uses", "")) for step in build["steps"]]
    compare = next(
        (i for i, s in enumerate(build["steps"]) if "compare_images.py" in s.get("run", "")), None
    )
    if compare is None:
        return ["release build does not compare its image with the smoke-tested one"]
    run = build["steps"][compare]["run"]
    found = []
    if "--against-manifest" not in run or "gh run download" not in run:
        found.append("release build does not compare against the smoke manifest of its CI run")
    if build.get("permissions", {}).get("actions") != "read":
        found.append("release build cannot read the CI run's smoke manifest")
    export = next(i for i, name in enumerate(names) if "Export scanned image" in name)
    if compare >= export:
        found.append("release build exports the image before comparing it")
    return found


def smoke_manifest_violations(ci: dict) -> list[str]:
    smoke = ci["jobs"].get("smoke")
    if smoke is None:
        return []
    found = []
    upload = next(
        (s for s in smoke["steps"] if "actions/upload-artifact@" in s.get("uses", "")), None
    )
    if upload is None or upload["with"].get("name") != "smoke-image-manifest":
        found.append("smoke does not keep the manifest of the image it tested")
    elif upload["with"].get("if-no-files-found") != "error":
        found.append("smoke manifest upload tolerates a missing manifest")
    runner = next((s for s in smoke["steps"] if "uv python install" in s.get("run", "")), None)
    if runner is None or "HEALTHOS_SMOKE_MANIFEST" not in runner.get("env", {}):
        found.append("the smoke run is not told to record the image it tested")
    return found


def release_provenance_violations(jobs: dict) -> list[str]:
    found = []
    attest = jobs.get("attest", {"steps": []})
    if not any("actions/attest-build-provenance@" in s.get("uses", "") for s in attest["steps"]):
        found.append("release has no build provenance attestation")
    verifier = jobs.get("verify-provenance", {"steps": []})
    commands = " ".join(s.get("run", "") for s in verifier["steps"])
    for required in ("gh attestation verify", "--signer-workflow", "--source-ref"):
        if required not in commands:
            found.append(f"release provenance check lost {required}")
    return found


def release_permissions_violations(jobs: dict) -> list[str]:
    found = []
    for name, job in jobs.items():
        permissions = job.get("permissions", {})
        if permissions.get("contents") == "write" and name != "publish":
            found.append(f"contents: write on release job {name}")
        if permissions.get("id-token") == "write" and name != "attest":
            found.append(f"id-token: write on release job {name}")
    if "publish" in jobs:
        foreign = [
            s["uses"]
            for s in jobs["publish"]["steps"]
            if "uses" in s and not s["uses"].startswith("actions/download-artifact@")
        ]
        if foreign:
            found.append(f"publish holds contents: write and runs {foreign}")
    return found


def protection_violations(files: Files) -> list[str]:
    found = []
    ruleset = files.main_ruleset
    if ruleset.get("enforcement") != "active" or ruleset.get("target") != "branch":
        found.append("main ruleset is not an active branch ruleset")
    if "refs/heads/main" not in ruleset.get("conditions", {}).get("ref_name", {}).get(
        "include", []
    ):
        found.append("main ruleset does not cover main")
    checks = [
        check["context"]
        for rule in ruleset.get("rules", [])
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    ]
    missing = REQUIRED_CONTEXTS - set(checks)
    if missing:
        found.append(f"main ruleset does not require {sorted(missing)}")
    strict = [
        rule["parameters"].get("strict_required_status_checks_policy")
        for rule in ruleset.get("rules", [])
        if rule["type"] == "required_status_checks"
    ]
    if strict != [True]:
        found.append("main ruleset does not require up-to-date branches (strict)")
    if ruleset.get("bypass_actors") != EXPECTED_BYPASS:
        found.append(
            "main ruleset bypass changed; a maintainer decision, see .github/rulesets/README.md"
        )
    if not any(
        r["type"] == "pull_request" and r["parameters"]["require_code_owner_review"]
        for r in ruleset.get("rules", [])
    ):
        found.append("main ruleset does not require code-owner review")
    tag = files.tag_ruleset
    if tag.get("enforcement") != "active" or tag.get("target") != "tag":
        found.append("release tag ruleset is not active")
    if tag.get("bypass_actors") != EXPECTED_BYPASS:
        found.append("release tag ruleset permits a bypass")
    if {r["type"] for r in tag.get("rules", [])} != {"update", "deletion"}:
        found.append("release tags must allow creation and prohibit updates and deletion")
    if not re.search(r"^\*\s+@\S+", files.codeowners, re.MULTILINE):
        found.append("CODEOWNERS has no default owner")
    return found


def floor_violations(files: Files) -> list[str]:
    found = []
    makefile = files.makefile
    coverage = re.search(r"^coverage:.*\n\t(.*)$", makefile, re.MULTILINE)
    if coverage is None or "--cov=app" not in coverage.group(1):
        found.append("make coverage does not measure app/")
    security = re.search(r"^coverage-security: coverage\n\t(.*\\\n.*)$", makefile, re.MULTILINE)
    if security is None or "--fail-under=$(SECURITY_FLOOR)" not in security.group(1):
        found.append("make coverage-security does not enforce its floor")
    floor = re.search(r"^SECURITY_FLOOR = (\d+)$", makefile, re.MULTILINE)
    if floor is None or int(floor.group(1)) < 90:
        found.append("the security coverage floor was lowered")
    report = tomllib.loads(files.pyproject)["tool"]["coverage"]["report"]
    if report.get("fail_under", 0) < 92.6:
        found.append("fail_under was lowered")
    combined = re.search(r"^ci-coverage-report:\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    required = (
        "coverage combine --keep .test-tmp-ci-coverage/.coverage.sqlite "
        ".test-tmp-ci-coverage/.coverage.postgres",
        "coverage report",
        "coverage report --include=$(SECURITY_MODULES) --fail-under=$(SECURITY_FLOOR)",
    )
    recipe = combined.group(1) if combined else ""
    commands = [
        line.strip().removeprefix("$(UV_RUN) ").removesuffix(" \\") for line in recipe.splitlines()
    ]
    if not all(command in commands for command in required):
        found.append("CI must combine both databases and enforce both existing coverage floors")
    postgres = re.search(r"^test-postgres:\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    recipe = postgres.group(1) if postgres else ""
    if "-m integration tests/integration" not in recipe:
        found.append("make test-postgres does not run tests/integration")
    if "HEALTHOS_TEST_BACKEND=postgres" not in recipe:
        found.append("make test-postgres does not run the unit suite on PostgreSQL")
    if recipe.count("HEALTHOS_REQUIRE_POSTGRES=1") != 2:
        found.append("make test-postgres may skip without a server")
    found.extend(recipe_violations(makefile))
    return found


def recipe_violations(makefile: str) -> list[str]:
    """The Make targets CI runs carry the same bans as a workflow's ``run`` block."""
    found = []
    for target in (
        "test",
        "test-e2e",
        "test-clock",
        "test-clock-preflight",
        "test-postgres",
        "coverage",
        "ci-coverage-report",
    ):
        match = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", makefile, re.MULTILINE)
        if match is None:
            found.append(f"make {target} has no recipe")
            continue
        for line in match.group(1).splitlines():
            # The only exclusion a recipe may hold: the PostgreSQL unit run leaves the integration
            # package to the line above it.
            command = line.replace("--ignore=tests/integration", "")
            if FORBIDDEN_IN_RUN.search(command):
                found.append(f"make {target} weakens a gate: {command.strip()}")
    return found


def clock_harness_violations(files: Files) -> list[str]:
    found = []
    if "--with" in files.clock_matrix.replace("`uv run --with`", ""):
        found.append("clock_matrix.sh uses a uv overlay, which the pytest script cannot see")
    if "uv pip install" in files.clock_matrix or "--target" in files.clock_matrix:
        found.append("clock_matrix.sh installs unlocked dependencies outside the environment")
    if 'PYTHONPATH="tests/support"' not in files.clock_matrix:
        found.append("clock_matrix.sh does not put the clock plugin on PYTHONPATH")
    if not re.search(r"^preflight \|\| exit", files.clock_matrix, re.MULTILINE):
        found.append("clock_matrix.sh runs the settings without proving the harness first")
    return found


def violations(files: Files) -> list[str]:
    return [
        *ci_violations(files.ci),
        *release_violations(files.release),
        *protection_violations(files),
        *floor_violations(files),
        *python_pin_violations(files),
        *schedule_violations(files),
        *clock_harness_violations(files),
    ]


def test_repository_satisfies_the_gate_policy():
    assert violations(load()) == []


def test_every_action_is_pinned_by_sha_and_names_its_version():
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*(- )?uses:", line):
                assert re.search(r"@[a-f0-9]{40} # v\d+\.\d+\.\d+$", line), f"{path.name}: {line}"


def step_named(job: dict, fragment: str) -> dict:
    return next(
        s for s in job["steps"] if fragment in s.get("run", "") or fragment in s.get("uses", "")
    )


def drop_step(job: dict, fragment: str) -> None:
    job["steps"].remove(step_named(job, fragment))


def in_ci(change: Callable[[dict], None]) -> Callable[[Files], Files]:
    def mutate(files: Files) -> Files:
        ci = copy.deepcopy(files.ci)
        change(ci["jobs"])
        return replace(files, ci=ci)

    return mutate


def in_release(change: Callable[[dict], None]) -> Callable[[Files], Files]:
    def mutate(files: Files) -> Files:
        release = copy.deepcopy(files.release)
        change(release["jobs"])
        return replace(files, release=release)

    return mutate


def sets(mapping_key: str, key: str, value: object) -> Callable[[dict], None]:
    def change(jobs: dict) -> None:
        jobs[mapping_key][key] = value

    return change


def drops_need(gate: str) -> Callable[[dict], None]:
    def change(jobs: dict) -> None:
        jobs["summary"]["needs"].remove(gate)

    return change


def rewrites_run(job: str, fragment: str, old: str, new: str) -> Callable[[dict], None]:
    def change(jobs: dict) -> None:
        step = step_named(jobs[job], fragment)
        step["run"] = step["run"].replace(old, new)

    return change


def step_setting(job: str, fragment: str, key: str, value: object) -> Callable[[dict], None]:
    def change(jobs: dict) -> None:
        step_named(jobs[job], fragment)[key] = value

    return change


def trivy_exit_zero(job: str) -> Callable[[dict], None]:
    def change(jobs: dict) -> None:
        step_named(jobs[job], "trivy-action")["with"]["exit-code"] = "0"

    return change


def in_workflow(field: str, change: Callable[[dict], None]) -> Callable[[Files], Files]:
    def mutate(files: Files) -> Files:
        workflow = copy.deepcopy(getattr(files, field))
        change(workflow)
        return replace(files, **{field: workflow})

    return mutate


def mutate_file(field: str, old: str, new: str) -> Callable[[Files], Files]:
    def mutate(files: Files) -> Files:
        assert old in getattr(files, field)
        return replace(files, **{field: getattr(files, field).replace(old, new)})

    return mutate


def mutate_ruleset(change: Callable[[dict], None]) -> Callable[[Files], Files]:
    def mutate(files: Files) -> Files:
        ruleset = copy.deepcopy(files.main_ruleset)
        change(ruleset)
        return replace(files, main_ruleset=ruleset)

    return mutate


def not_strict(ruleset: dict) -> None:
    next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")["parameters"][
        "strict_required_status_checks_policy"
    ] = False


def drops_codeql_context(ruleset: dict) -> None:
    parameters = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")[
        "parameters"
    ]
    parameters["required_status_checks"] = [
        c for c in parameters["required_status_checks"] if "CodeQL" not in c["context"]
    ]


def adds_bypass(ruleset: dict) -> None:
    ruleset["bypass_actors"].append({"actor_id": 1, "actor_type": "Team", "bypass_mode": "always"})


def bypass_mode_changed(ruleset: dict) -> None:
    ruleset["bypass_actors"] = [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
    ]


def no_required_summary(ruleset: dict) -> None:
    ruleset["rules"] = [r for r in ruleset["rules"] if r["type"] != "required_status_checks"]


def evaluate_mode(ruleset: dict) -> None:
    ruleset["enforcement"] = "evaluate"


def cancels_on_main(files: Files) -> Files:
    ci = copy.deepcopy(files.ci)
    ci["concurrency"]["cancel-in-progress"] = True
    return replace(files, ci=ci)


def any_tag(files: Files) -> Files:
    release = copy.deepcopy(files.release)
    release["on"]["push"]["tags"] = ["**"]
    return replace(files, release=release)


def default_token(files: Files) -> Files:
    release = copy.deepcopy(files.release)
    release["permissions"] = {"contents": "write"}
    return replace(files, release=release)


def paths_filter(files: Files) -> Files:
    ci = copy.deepcopy(files.ci)
    ci["on"]["push"]["paths"] = ["docs/**"]
    return replace(files, ci=ci)


def drop_summary_gate_check(jobs: dict) -> None:
    run = jobs["summary"]["steps"][0]["run"]
    jobs["summary"]["steps"][0]["run"] = run.replace(' "$SECRETS"', "")


def third_party_in_publish(jobs: dict) -> None:
    jobs["publish"]["steps"].insert(0, {"uses": "someone/action@" + "a" * 40})


def build_may_write(jobs: dict) -> None:
    jobs["build"]["permissions"] = {"contents": "write"}


def drop_attestation(jobs: dict) -> None:
    drop_step(jobs["attest"], "attest-build-provenance")


MUTANTS = {
    **{f"summary-without-{gate}": in_ci(drops_need(gate)) for gate in GATES},
    **{f"continue-on-error-{gate}": in_ci(sets(gate, "continue-on-error", True)) for gate in GATES},
    **{f"disabled-{gate}": in_ci(sets(gate, "if", "${{ false }}")) for gate in GATES},
    "summary-not-always": in_ci(lambda jobs: jobs["summary"].pop("if")),
    "summary-continue-on-error": in_ci(sets("summary", "continue-on-error", True)),
    "summary-loop-skips-secrets": in_ci(drop_summary_gate_check),
    "python-312-experimental": in_ci(
        lambda jobs: jobs["verify"]["strategy"]["matrix"]["include"][0].update(experimental=True)
    ),
    "unit-tests-collect-only": in_ci(
        rewrites_run("verify", "make coverage-security", "make coverage-security", "pytest --co")
    ),
    "coverage-floor-removed": in_ci(
        rewrites_run("verify", "make coverage-security", "make coverage-security", "make test")
    ),
    "integration-removed": in_ci(lambda jobs: drop_step(jobs["verify"], "make test-postgres")),
    "integration-tolerated": in_ci(
        rewrites_run("verify", "make test-postgres", "test-postgres", "test-postgres || true")
    ),
    "postgres-coverage-disabled": in_ci(step_setting("verify", "make test-postgres", "env", {})),
    "postgres-coverage-overwrites-sqlite": in_ci(
        step_setting("verify", "make test-postgres", "env", {"PYTEST_ADDOPTS": "--cov=app"})
    ),
    "combined-coverage-report-removed": in_ci(
        lambda jobs: drop_step(jobs["verify"], "make ci-coverage-report")
    ),
    "combined-coverage-before-postgres": in_ci(
        lambda jobs: jobs["verify"]["steps"].insert(
            0,
            jobs["verify"]["steps"].pop(
                jobs["verify"]["steps"].index(step_named(jobs["verify"], "make ci-coverage-report"))
            ),
        )
    ),
    "combined-coverage-only-sqlite": mutate_file(
        "makefile",
        "combine --keep .test-tmp-ci-coverage/.coverage.sqlite "
        ".test-tmp-ci-coverage/.coverage.postgres",
        "combine --keep .test-tmp-ci-coverage/.coverage.sqlite",
    ),
    "combined-security-floor-removed": mutate_file(
        "makefile",
        "\t$(UV_RUN) coverage report --include=$(SECURITY_MODULES) --fail-under=$(SECURITY_FLOOR)",
        "\t$(UV_RUN) coverage report --include=$(SECURITY_MODULES)",
    ),
    "unit-step-disabled": in_ci(
        step_setting("verify", "make coverage-security", "if", "${{ false }}")
    ),
    "unit-step-continue": in_ci(
        step_setting("verify", "make coverage-security", "continue-on-error", True)
    ),
    "eslint-removed": in_ci(lambda jobs: drop_step(jobs["browser"], "npm run lint")),
    "e2e-journey-removed": in_ci(lambda jobs: drop_step(jobs["e2e"], "make test-e2e")),
    "e2e-journey-tolerated": in_ci(
        rewrites_run("e2e", "make test-e2e", "test-e2e", "test-e2e || true")
    ),
    "e2e-job-removed": in_ci(lambda jobs: jobs.pop("e2e")),
    "bridge-boundary-job-removed": in_ci(lambda jobs: jobs.pop("bridge_boundary")),
    "bridge-boundary-tests-removed": in_ci(
        lambda jobs: drop_step(jobs["bridge_boundary"], "pytest -q")
    ),
    "e2e-out-of-the-summary": in_ci(drops_need("e2e")),
    "pip-audit-unpinned": in_ci(
        rewrites_run("security", "pip-audit", "pip-audit==2.10.1", "pip-audit")
    ),
    "gitleaks-history-removed": in_ci(
        rewrites_run("secrets", "gitleaks git", 'gitleaks git . --log-opts="--all" --redact', ":")
    ),
    "ci-trivy-exit-zero": in_ci(trivy_exit_zero("security")),
    "ci-cancels-main": cancels_on_main,
    "ci-paths-filter": paths_filter,
    "release-any-tag": any_tag,
    "release-default-token": default_token,
    "release-trivy-exit-zero": in_release(trivy_exit_zero("build")),
    "release-gate-removed": in_release(lambda jobs: jobs["build"].pop("needs")),
    "release-ci-check-removed": in_release(lambda jobs: drop_step(jobs["gate"], "ci --commit")),
    "release-preflight-removed": in_release(lambda jobs: drop_step(jobs["gate"], "preflight")),
    "release-attestation-removed": in_release(drop_attestation),
    "release-build-can-write": in_release(build_may_write),
    "release-publish-third-party": in_release(third_party_in_publish),
    "ruleset-not-strict": mutate_ruleset(not_strict),
    "ruleset-without-codeql": mutate_ruleset(drops_codeql_context),
    "ruleset-extra-bypass": mutate_ruleset(adds_bypass),
    "tag-ruleset-bypass": lambda f: replace(
        f,
        tag_ruleset={
            **f.tag_ruleset,
            "bypass_actors": [
                {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
            ],
        },
    ),
    "tag-ruleset-blocks-creation": lambda f: replace(
        f, tag_ruleset={**f.tag_ruleset, "rules": [*f.tag_ruleset["rules"], {"type": "creation"}]}
    ),
    "ruleset-bypass-mode": mutate_ruleset(bypass_mode_changed),
    "ruleset-no-bypass-recorded": mutate_ruleset(lambda r: r.pop("bypass_actors")),
    "gitleaks-x64-digest-changed": in_ci(
        rewrites_run("secrets", "gitleaks.tar.gz", GITLEAKS_X64_SHA256, "0" * 64)
    ),
    "gitleaks-no-retry": in_ci(
        rewrites_run("secrets", "gitleaks.tar.gz", " --retry 5 --retry-all-errors", "")
    ),
    "ci-python-floats": in_ci(
        lambda jobs: jobs["verify"]["strategy"]["matrix"]["include"][0].update(python="3.12")
    ),
    "ci-install-floats": mutate_file(
        "ci_text", "uv python install 3.12.16", "uv python install 3.12"
    ),
    "ci-gate-other-python": mutate_file("ci_gate", "python:3.12.16", "python:3.12"),
    "image-no-comparison": in_ci(
        rewrites_run(
            "security", "compare_images.py", "python3 .github/scripts/compare_images.py", "echo"
        )
    ),
    "image-no-epoch": in_ci(
        rewrites_run("security", "compare_images.py", ' --build-arg SOURCE_DATE_EPOCH="$epoch"', "")
    ),
    "codeql-findings-tolerated": in_ci(lambda jobs: drop_step(jobs["codeql"], "codeql_gate.py")),
    "codeql-language-dropped": in_ci(
        lambda jobs: jobs["codeql"]["strategy"]["matrix"].update(language=["python"])
    ),
    "codeql-init-removed": in_ci(lambda jobs: drop_step(jobs["codeql"], "codeql-action/init")),
    "smoke-removed": in_ci(lambda jobs: drop_step(jobs["smoke"], "make compose-smoke")),
    "smoke-silent-under-act": in_ci(lambda jobs: drop_step(jobs["smoke"], "::warning::")),
    "act-skip-on-verify": in_ci(
        step_setting("verify", "make test-postgres", "if", "${{ !env.ACT }}")
    ),
    "act-forged-on-github": in_ci(lambda jobs: jobs["smoke"]["env"].update(ACT="true")),
    "audit-no-schedule": in_workflow("audit", lambda w: w["on"].pop("schedule")),
    "audit-pip-removed": in_workflow(
        "audit",
        lambda w: drop_step(w["jobs"]["audit"], "pip-audit"),
    ),
    "audit-trivy-exit-zero": in_workflow("audit", lambda w: trivy_exit_zero("audit")(w["jobs"])),
    "audit-step-tolerated": in_workflow(
        "audit",
        lambda w: step_setting("audit", "npm audit", "continue-on-error", True)(w["jobs"]),
    ),
    "clock-no-schedule": in_workflow("clock", lambda w: w["on"].pop("schedule")),
    "clock-not-run": in_workflow(
        "clock",
        lambda w: rewrites_run("clock", "make test-clock", "make test-clock", "true")(w["jobs"]),
    ),
    "codeql-duplicate-trigger": in_workflow("codeql", lambda w: w["on"].update(pull_request=None)),
    "release-provenance-unchecked": in_release(
        lambda jobs: drop_step(jobs["verify-provenance"], "gh attestation verify")
    ),
    "release-publishes-unverified": in_release(lambda jobs: jobs["publish"].update(needs="attest")),
    "ruleset-without-summary": mutate_ruleset(no_required_summary),
    "ruleset-evaluate-only": mutate_ruleset(evaluate_mode),
    "codeowners-empty": mutate_file("codeowners", "* @M3rgen", "# nobody"),
    "security-floor-lowered": mutate_file("makefile", "SECURITY_FLOOR = 90", "SECURITY_FLOOR = 1"),
    "security-floor-dropped": mutate_file("makefile", " --fail-under=$(SECURITY_FLOOR)", ""),
    "coverage-not-measured": mutate_file("makefile", " --cov=app --cov-report=term", ""),
    "postgres-integration-dropped": mutate_file(
        "makefile", " -m integration tests/integration", " -m integration tests/test_sources.py"
    ),
    "postgres-unit-on-sqlite": mutate_file("makefile", "HEALTHOS_TEST_BACKEND=postgres ", ""),
    "postgres-may-skip": mutate_file("makefile", "HEALTHOS_REQUIRE_POSTGRES=1 ", ""),
    "fail-under-lowered": mutate_file("pyproject", "fail_under = 92.6", "fail_under = 10"),
    "postgres-unit-selected-away": mutate_file(
        "makefile",
        '-m "not integration" --ignore',
        '-k nothing_at_all -m "not integration" --ignore',
    ),
    "postgres-integration-selected-away": mutate_file(
        "makefile",
        "-m integration tests/integration",
        "-k nothing -m integration tests/integration",
    ),
    "postgres-unit-ignores-more": mutate_file(
        "makefile", "--ignore=tests/integration", "--ignore=tests/integration --ignore=tests/unit"
    ),
    "coverage-selected-away": mutate_file(
        "makefile", '-m "not integration" --cov=app', '-k nothing -m "not integration" --cov=app'
    ),
    "history-scan-shallow": in_ci(
        lambda jobs: step_named(jobs["secrets"], "actions/checkout@")["with"].update(
            {"fetch-depth": 1}
        )
    ),
    "history-scan-default-depth": in_ci(
        lambda jobs: step_named(jobs["secrets"], "actions/checkout@")["with"].pop("fetch-depth")
    ),
    "clock-preflight-removed": in_ci(
        rewrites_run("verify", "make test-clock-preflight", "make test-clock-preflight", "true")
    ),
    "clock-overlay-back": mutate_file(
        "clock_matrix",
        "uv run --frozen --extra dev pytest -p",
        "uv run --frozen --extra dev --with time-machine pytest -p",
    ),
    "clock-plugin-not-on-path": mutate_file(
        "clock_matrix", 'PYTHONPATH="tests/support"', 'PYTHONPATH=""'
    ),
    "clock-no-preflight-first": mutate_file("clock_matrix", "preflight || exit 2\n", ""),
    "release-not-compared-with-smoke": in_release(
        lambda jobs: drop_step(jobs["build"], "--against-manifest")
    ),
    "release-compare-permission-lost": in_release(
        lambda jobs: jobs["build"]["permissions"].pop("actions")
    ),
    "release-compare-after-export": in_release(
        lambda jobs: jobs["build"]["steps"].append(
            jobs["build"]["steps"].pop(
                next(
                    i
                    for i, s in enumerate(jobs["build"]["steps"])
                    if "--against-manifest" in s.get("run", "")
                )
            )
        )
    ),
    "smoke-manifest-not-kept": in_ci(
        lambda jobs: drop_step(jobs["smoke"], "actions/upload-artifact")
    ),
    "smoke-manifest-not-written": in_ci(
        lambda jobs: step_named(jobs["smoke"], "uv python install")["env"].pop(
            "HEALTHOS_SMOKE_MANIFEST"
        )
    ),
    "smoke-manifest-optional": in_ci(
        lambda jobs: step_named(jobs["smoke"], "actions/upload-artifact")["with"].update(
            {"if-no-files-found": "ignore"}
        )
    ),
    "publish-no-approval": in_release(lambda jobs: jobs["publish"].pop("environment")),
}


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_weakened_gate_is_reported(name):
    assert violations(MUTANTS[name](load())), f"{name} was not reported"


def test_gates_disabled_together_are_each_reported():
    files = load()
    for name in (
        "summary-without-secrets",
        "continue-on-error-verify",
        "unit-tests-collect-only",
        "release-trivy-exit-zero",
        "ruleset-evaluate-only",
    ):
        files = MUTANTS[name](files)
    found = "\n".join(violations(files))
    for expected in (
        "summary needs",
        "gate job verify has continue-on-error",
        "weakens a gate: pytest --co",
        "release.yml build: Trivy does not fail",
        "main ruleset is not an active",
    ):
        assert expected in found


def test_every_integration_test_module_carries_the_marker():
    for path in sorted((ROOT / "tests/integration").glob("test_*.py")):
        assert re.search(r"^pytestmark\b", path.read_text(encoding="utf-8"), re.MULTILINE), path
