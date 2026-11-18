"""scripts/hooks/pre-push: the snapshot may reach origin on a version-named candidate branch."""

from pathlib import Path

import pytest
from test_branch_scripts import Repo
from test_branch_scripts import repo as repo
from test_github_publication import push, record_gate, scanner
from test_pre_push_release_tags import scanner_log

ZERO = "0" * 40


def candidate(repo: Repo, version: str = "0.2.1") -> str:
    """A snapshot commit that carries pyproject ``version``."""
    repo.commit("release", {"pyproject.toml": f'[project]\nname = "x"\nversion = "{version}"\n'})
    commit = repo.git("rev-parse", "HEAD")
    repo.git("branch", "-f", "github-main", commit)
    return commit


def line(oid: str, ref: str = "refs/heads/snapshot/v0.2.1", remote: str = ZERO) -> str:
    return f"refs/heads/github-main {oid} {ref} {remote}\n"


def test_candidate_needs_receipt_and_is_scanned_like_main(repo: Repo) -> None:
    oid = candidate(repo)
    missing = push(repo, line(oid))
    assert missing.returncode == 1 and "no CI gate receipt" in missing.stderr
    assert scanner_log(repo) == ""
    record_gate(repo, oid)
    result = push(repo, line(oid))
    assert result.returncode == 0, result.stderr
    assert f"--log-opts={oid}\n" in scanner_log(repo)  # new remote branch: whole history


def test_candidate_update_scans_the_outgoing_range(repo: Repo) -> None:
    old = repo.git("rev-parse", "main")
    oid = candidate(repo)
    record_gate(repo, oid)
    result = push(repo, line(oid, remote=old))
    assert result.returncode == 0, result.stderr
    assert f"--log-opts={old}..{oid}\n" in scanner_log(repo)


def test_scanner_failure_blocks_the_candidate(repo: Repo) -> None:
    oid = candidate(repo)
    record_gate(repo, oid)
    scanner(repo).write_text("#!/bin/sh\nexit 1\n")
    result = push(repo, line(oid))
    assert result.returncode == 1 and "gitleaks rejected" in result.stderr


@pytest.mark.parametrize("version", ["0.2.2", "0.2.1.1"])
def test_candidate_name_must_match_project_version(repo: Repo, version: str) -> None:
    oid = candidate(repo)
    record_gate(repo, oid)
    result = push(repo, line(oid, ref=f"refs/heads/snapshot/v{version}"))
    assert result.returncode == 1 and "does not match project version 0.2.1" in result.stderr
    assert scanner_log(repo) == ""


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/snapshot/0.2.1",
        "refs/heads/snapshot/v0.2.1-rc1",
        "refs/heads/snapshot/v0.2.1/x",
        "refs/heads/snapshot",
        "refs/heads/candidate/v0.2.1",
        "refs/tags/snapshot/v0.2.1",
    ],
)
def test_other_branch_names_are_refused(repo: Repo, ref: str) -> None:
    oid = candidate(repo)
    record_gate(repo, oid)
    result = push(repo, line(oid, ref=ref))
    assert result.returncode == 1 and "origin accepts only" in result.stderr
    assert scanner_log(repo) == ""


def test_candidate_comes_only_from_the_snapshot_branch(repo: Repo) -> None:
    oid = candidate(repo)
    record_gate(repo, oid)
    for local in ("refs/heads/dev", "refs/heads/main", "refs/heads/snapshot/v0.2.1"):
        result = push(repo, f"{local} {oid} refs/heads/snapshot/v0.2.1 {ZERO}\n")
        assert result.returncode == 1 and "origin accepts only" in result.stderr
    assert scanner_log(repo) == ""


def test_merged_candidate_may_be_deleted_and_nothing_else(repo: Repo) -> None:
    oid = candidate(repo)
    deleted = push(repo, f"(delete) {ZERO} refs/heads/snapshot/v0.2.1 {oid}\n")
    assert deleted.returncode == 0, deleted.stderr
    assert not (Path(repo.env["HOME"]) / "scanner-args").exists()
    for ref in ("refs/heads/main", "refs/heads/other", "refs/tags/v0.2.1"):
        result = push(repo, f"(delete) {ZERO} {ref} {oid}\n")
        assert result.returncode == 1 and "origin accepts only" in result.stderr
