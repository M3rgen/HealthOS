"""scripts/ci_gate.sh: a receipt exists only after every ci.yml job passed for that exact tree."""

import subprocess
from pathlib import Path

import pytest
from test_branch_scripts import ROOT, Repo
from test_branch_scripts import repo as repo
from test_github_publication import push

FAKE_ACT = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "act version fake"; exit 0; fi
printf '%s\\n' "$@" > "$HOME/act-args"
pwd > "$HOME/act-cwd"
git rev-parse HEAD > "$HOME/act-head"
git symbolic-ref HEAD > "$HOME/act-branch"
git for-each-ref --format='%(refname)' > "$HOME/act-refs"
ls -a > "$HOME/act-files"
exit "${FAKE_ACT_EXIT:-0}"
"""
FAKE_DOCKER = """#!/bin/sh
case "$1 $2" in
  "context inspect") echo unix:///fake.sock ;;
  "info --format") echo aarch64 ;;
esac
"""


@pytest.fixture
def gate_repo(repo: Repo) -> Repo:
    bin_dir = Path(repo.env["HOME"]) / "bin"
    for name, body in (("act", FAKE_ACT), ("docker", FAKE_DOCKER)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    repo.commit(
        "workflow",
        {
            ".github/workflows/ci.yml": "name: ci\n",
            "scripts/ci_gate.sh": (ROOT / "scripts/ci_gate.sh").read_text(encoding="utf-8"),
        },
    )
    repo.git("branch", "-f", "github-main", "HEAD")
    repo.git("remote", "add", "origin", "https://github.com/example/healthos.git")
    return repo


def run_gate(repo: Repo, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/ci_gate.sh", *args],
        cwd=repo.path,
        env={**repo.env, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def receipt(repo: Repo, commit: str) -> Path:
    return repo.path / repo.git("rev-parse", "--git-common-dir") / "healthos-ci-gate" / commit


def home(repo: Repo, name: str) -> str:
    return (Path(repo.env["HOME"]) / name).read_text()


def test_passing_gate_records_the_tree_and_opens_the_hook(gate_repo: Repo) -> None:
    commit = gate_repo.git("rev-parse", "github-main")
    before = gate_repo.git("rev-parse", f"{commit}~1")
    update = f"refs/heads/github-main {commit} refs/heads/main {before}\n"
    assert push(gate_repo, update).returncode == 1
    result = run_gate(gate_repo)
    assert result.returncode == 0, result.stderr
    tree = gate_repo.git("rev-parse", f"{commit}^{{tree}}")
    assert receipt(gate_repo, commit).read_text().startswith(f"tree {tree}\n")
    # A receipt must say what act could not exercise, not imply it passed.
    assert (
        "not-run-under-act codeql smoke installer_lint installer_hosts installer_linux_docker"
        " installer_windows_ps51\n"
    ) in receipt(gate_repo, commit).read_text()
    assert push(gate_repo, update).returncode == 0


def test_act_runs_the_whole_workflow_on_a_pristine_main_checkout(gate_repo: Repo) -> None:
    gate_repo.write("uncommitted.txt", "local state\n")
    gate_repo.git("branch", "private/secret-work")
    assert run_gate(gate_repo).returncode == 0
    args = home(gate_repo, "act-args").splitlines()
    assert args[:3] == ["push", "-W", ".github/workflows/ci.yml"]
    assert "--job" not in args and "-j" not in args  # no job may be left out of the gate
    assert args[args.index("--matrix") + 1] == "python:3.12.16"  # the blocking entry
    assert "--cache-server-path" in args  # no cache carried over from another run
    assert home(gate_repo, "act-head").strip() == gate_repo.git("rev-parse", "github-main")
    assert home(gate_repo, "act-branch").strip() == "refs/heads/main"
    assert home(gate_repo, "act-refs").split() == ["refs/heads/main"]
    assert "uncommitted.txt" not in home(gate_repo, "act-files")
    assert not Path(home(gate_repo, "act-cwd").strip()).exists()


def test_failed_job_writes_no_receipt(gate_repo: Repo) -> None:
    commit = gate_repo.git("rev-parse", "github-main")
    result = run_gate(gate_repo, FAKE_ACT_EXIT="1")
    assert result.returncode == 1 and "no receipt" in result.stderr
    assert not receipt(gate_repo, commit).exists()


def test_receipt_is_for_one_commit_only(gate_repo: Repo) -> None:
    assert run_gate(gate_repo).returncode == 0
    old = gate_repo.git("rev-parse", "github-main")
    gate_repo.commit("later", {"notes/later.txt": "x\n"})
    new = gate_repo.git("rev-parse", "HEAD")
    gate_repo.git("branch", "-f", "github-main", new)
    result = push(gate_repo, f"refs/heads/github-main {new} refs/heads/main {old}\n")
    assert result.returncode == 1 and "no CI gate receipt" in result.stderr


def test_tampered_receipt_tree_is_refused(gate_repo: Repo) -> None:
    assert run_gate(gate_repo).returncode == 0
    commit = gate_repo.git("rev-parse", "github-main")
    receipt(gate_repo, commit).write_text("tree " + "0" * 40 + "\n")
    before = gate_repo.git("rev-parse", f"{commit}~1")
    result = push(gate_repo, f"refs/heads/github-main {commit} refs/heads/main {before}\n")
    assert result.returncode == 1 and "no CI gate receipt" in result.stderr


def test_missing_tools_and_bad_revision_are_usage_errors(gate_repo: Repo) -> None:
    assert run_gate(gate_repo, "no-such-revision").returncode == 2
    (Path(gate_repo.env["HOME"]) / "bin/docker").write_text("#!/bin/sh\nexit 1\n")
    result = run_gate(gate_repo)
    assert result.returncode == 2 and "Docker" in result.stderr
