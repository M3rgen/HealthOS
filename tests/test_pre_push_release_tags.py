"""scripts/hooks/pre-push: only well-formed, version-matching release tags reach origin."""

import shutil
import subprocess
from pathlib import Path

import pytest
from support.release_tags import RELEASE_TAGS
from test_branch_scripts import Repo
from test_branch_scripts import repo as repo
from test_github_publication import push, record_gate


def scanner_log(repo: Repo) -> str:
    path = Path(repo.env["HOME"]) / "scanner-args"
    return path.read_text() if path.exists() else ""


def tagged(repo: Repo, version: str = "0.2.1", annotated: bool = False) -> tuple[str, str]:
    """A commit that carries pyproject ``version`` and is on the snapshot branch."""
    repo.commit("release", {"pyproject.toml": f'[project]\nname = "x"\nversion = "{version}"\n'})
    commit = repo.git("rev-parse", "HEAD")
    repo.git("branch", "-f", "github-main", commit)
    name = f"refs/tags/v{version}"
    if annotated:
        repo.git("tag", "-a", "-m", "synthetic note", f"v{version}", commit)
        return name, repo.git("rev-parse", name)
    return name, commit


def line(ref: str, oid: str, remote: str = "0" * 40) -> str:
    return f"{ref} {oid} {ref} {remote}\n"


def test_well_formed_release_tag_needs_receipt_and_is_scanned(repo: Repo) -> None:
    ref, oid = tagged(repo)
    missing = push(repo, line(ref, oid))
    assert missing.returncode == 1 and "no CI gate receipt" in missing.stderr
    assert scanner_log(repo) == ""
    record_gate(repo, oid)
    result = push(repo, line(ref, oid))
    assert result.returncode == 0, result.stderr
    assert f"--log-opts={oid}\n" in scanner_log(repo)


def test_annotated_tag_message_is_scanned(repo: Repo) -> None:
    ref, oid = tagged(repo, annotated=True)
    commit = repo.git("rev-parse", f"{oid}^{{commit}}")
    record_gate(repo, commit)
    result = push(repo, line(ref, oid))
    assert result.returncode == 0, result.stderr
    assert "stdin" in scanner_log(repo).splitlines()


def test_scanner_failure_blocks_the_tag(repo: Repo) -> None:
    ref, oid = tagged(repo)
    record_gate(repo, oid)
    (Path(repo.env["HOME"]) / "bin/gitleaks").write_text("#!/bin/sh\nexit 1\n")
    result = push(repo, line(ref, oid))
    assert result.returncode == 1 and "gitleaks rejected" in result.stderr


@pytest.mark.parametrize("name, accepted", RELEASE_TAGS)
def test_release_tag_format(repo: Repo, name: str, accepted: bool) -> None:
    _, oid = tagged(repo, version=name[1:] if accepted else "1.2.3")
    record_gate(repo, oid)
    result = push(repo, line(f"refs/tags/{name}", oid))
    if accepted:
        assert result.returncode == 0, result.stderr
        assert f"--log-opts={oid}\n" in scanner_log(repo)
    else:
        assert result.returncode == 1 and "origin accepts only" in result.stderr
        assert scanner_log(repo) == ""


@pytest.mark.parametrize("ref", ["refs/tags/v0.2.2", "refs/tags/v0.2.1.1"])
def test_tag_must_match_project_version(repo: Repo, ref: str) -> None:
    _, oid = tagged(repo, version="0.2.1")
    record_gate(repo, oid)
    result = push(repo, line(ref, oid))
    assert result.returncode == 1 and "does not match project version 0.2.1" in result.stderr


def test_tag_must_be_on_the_snapshot_branch(repo: Repo) -> None:
    ref, oid = tagged(repo)
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("side", {"pyproject.toml": '[project]\nname = "x"\nversion = "0.2.1"\n# side\n'})
    side = repo.git("rev-parse", "side")
    record_gate(repo, side)
    assert side != oid
    result = push(repo, line(ref, side))
    assert result.returncode == 1 and "not on the published snapshot branch" in result.stderr


def test_existing_release_tag_is_immutable(repo: Repo) -> None:
    ref, oid = tagged(repo)
    record_gate(repo, oid)
    result = push(repo, line(ref, oid, remote="1" * 40))
    assert result.returncode == 1 and "immutable" in result.stderr


def test_tag_cannot_come_from_a_differently_named_local_ref(repo: Repo) -> None:
    ref, oid = tagged(repo)
    record_gate(repo, oid)
    result = push(repo, f"refs/heads/github-main {oid} {ref} {'0' * 40}\n")
    assert result.returncode == 1 and "origin accepts only" in result.stderr


def test_tag_deletion_is_refused(repo: Repo) -> None:
    ref, oid = tagged(repo)
    result = push(repo, f"(delete) {'0' * 40} {ref} {oid}\n")
    assert result.returncode == 1 and "origin accepts only" in result.stderr


def test_real_gitleaks_accepts_stdin_mode(repo: Repo) -> None:
    """The hook's annotated-tag path relies on `gitleaks stdin`; keep that subcommand honest."""
    scanner = shutil.which("gitleaks")
    if scanner is None:
        pytest.skip("gitleaks is not installed")
    result = subprocess.run(
        [scanner, "stdin", "--redact", "--no-banner"],
        input="harmless tag note\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
