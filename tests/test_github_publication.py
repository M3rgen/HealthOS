"""Public snapshot gates and the installed push hook in isolated repositories."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_branch_scripts import RELEASE, Repo
from test_branch_scripts import repo as repo

SCANNER = shutil.which("gitleaks")


def scanner(repo: Repo) -> Path:
    return Path(repo.env["HOME"]) / "bin/gitleaks"


def push(repo: Repo, updates: str, remote: str = "origin") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/hooks/pre-push", remote, "unused"],
        input=updates,
        cwd=repo.path,
        env=repo.env,
        capture_output=True,
        text=True,
        check=False,
    )


def record_gate(repo: Repo, oid: str) -> None:
    """What a passing `make ci-gate` leaves behind: scripts/ci_gate.sh's per-commit receipt."""
    receipt = Path(repo.path / repo.git("rev-parse", "--git-common-dir"), "healthos-ci-gate", oid)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(f"tree {repo.git('rev-parse', f'{oid}^{{tree}}')}\n")


def test_dry_run_checks_publication_tree_and_keeps_index(repo: Repo) -> None:
    repo.write("CHANGELOG.md", "uncommitted text must not be used\n")
    repo.git("add", "CHANGELOG.md")
    before = repo.git("write-tree"), repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert RELEASE in result.stdout
    assert before == (repo.git("write-tree"), repo.git("rev-parse", "github-main"))
    args = (Path(repo.env["HOME"]) / "scanner-args").read_text().splitlines()
    assert args[0] == "dir"
    assert not Path(args[1]).exists()  # temporary tree removed after scanning
    assert "--redact" in args and "--allow-leaks" not in args
    result = repo.run("github_snapshot.sh", "--override", "docs/BRANCHES.md=main")
    assert result.returncode == 0, result.stderr
    assert "docs/BRANCHES.md" not in repo.files("github-main")


@pytest.mark.parametrize("location", ["content", "path", "binary"])
def test_dry_run_forbidden_patterns_are_literal_multiple_values(repo: Repo, location: str) -> None:
    marker = "private.host-fixture"
    repo.git(
        "config", "--local", "--add", "healthos.githubForbidPattern", marker.replace(".", r"\.")
    )
    path = f"notes/{marker.upper()}.txt" if location == "path" else "notes/public.txt"
    content = "public text" if location == "path" else marker.upper()
    repo.commit("fixture", {path: "\0" + content if location == "binary" else content})
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--dry-run", "--allow-references")
    assert result.returncode == 1
    assert "forbidden pattern" in result.stderr
    assert repo.git("rev-parse", "github-main") == before
    repo.commit("clean", {path: "privateXhost-fixture"})
    if location != "path":
        assert repo.run("github_snapshot.sh", "--dry-run").returncode == 0


def test_configured_private_markers_stop_dry_run(repo: Repo) -> None:
    markers = (
        "192" + ".168.",
        "o" + "ba",
        "git" + "ea",
        "Claude" + "-Session",
        "codex@" + "example.invalid",
    )
    for marker in markers:
        repo.git("config", "--local", "--add", "healthos.githubForbidPattern", re.escape(marker))
    for marker in markers:
        repo.commit("private marker fixture", {"fixture.txt": marker})
        result = repo.run("github_snapshot.sh", "--dry-run")
        assert result.returncode == 1 and "forbidden pattern" in result.stderr


@pytest.mark.parametrize(
    "message", ["Update eklenti/private", "Release abc1234", "Fix " + "A2" * 32]
)
def test_dry_run_rejects_private_message_references(repo: Repo, message: str) -> None:
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", message)
    assert result.returncode == 1
    assert "commit message" in result.stderr
    assert repo.git("rev-parse", "github-main") == before
    repo.commit("changelog fixture", {"CHANGELOG.md": f"## Release\n\n- {message}\n"})
    assert repo.run("github_snapshot.sh", "--dry-run").returncode == 1


def test_snapshot_requires_changelog_patterns_and_scanner(repo: Repo) -> None:
    repo.commit("changelog fixture", {"CHANGELOG.md": "# No version section\n"})
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 2 and "heading and bullet list" in result.stderr
    repo.git("config", "--unset-all", "healthos.githubForbidPattern")
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", "Public release")
    assert result.returncode == 0, result.stderr
    repo.git("config", "healthos.githubForbidPattern", "[")
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", "Public release")
    assert result.returncode == 2 and "invalid regular expression" in result.stderr
    repo.git("config", "healthos.githubForbidPattern", "private-fixture")
    # Keep the normal tools available while deliberately omitting gitleaks.
    bin_dir = scanner(repo).parent
    scanner(repo).unlink()
    for name in ("git", "bash", "python3", "dirname", "mktemp", "rm", "head", "sed", "grep"):
        executable = shutil.which(name)
        assert executable
        (bin_dir / name).symlink_to(executable)
    repo.env["PATH"] = str(bin_dir)
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", "Public release")
    assert result.returncode == 2 and "gitleaks is required" in result.stderr
    oid = repo.git("rev-parse", "main")
    result = push(repo, f"refs/heads/github-main {oid} refs/heads/main {'0' * 40}\n")
    assert result.returncode == 1 and "gitleaks is required" in result.stderr


@pytest.mark.parametrize("location", ["content", "path", "binary"])
@pytest.mark.parametrize(
    "marker",
    ["192" + ".168.", "homelab" + ".local"],
)
def test_default_patterns_reject_private_markers(repo: Repo, marker: str, location: str) -> None:
    repo.git("config", "--unset-all", "healthos.githubForbidPattern")
    path = f"notes/{marker.upper().lstrip('/')}.txt" if location == "path" else "notes/fixture.txt"
    content = "public fixture" if location == "path" else marker.upper()
    repo.commit("synthetic marker", {path: "\0" + content if location == "binary" else content})
    before = repo.git("rev-parse", "github-main"), repo.git("write-tree")
    result = repo.run("github_snapshot.sh", "--dry-run", "--allow-references")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "forbidden pattern" in result.stderr
    assert path in result.stderr
    if location != "path":
        assert content not in result.stderr
    assert before == (repo.git("rev-parse", "github-main"), repo.git("write-tree"))


def test_configured_patterns_replace_defaults_and_list_all_findings(repo: Repo) -> None:
    repo.commit("synthetic marker", {"fixture.txt": "codex-" + "bot"})
    assert repo.run("github_snapshot.sh", "--dry-run").returncode == 0
    repo.git("config", "--add", "healthos.githubForbidPattern", "second-private-fixture")
    repo.commit(
        "synthetic markers",
        {"one.txt": "PRIVATE-HOST-FIXTURE", "two.txt": "SECOND-PRIVATE-FIXTURE"},
    )
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "one.txt" in result.stderr and "two.txt" in result.stderr
    assert "PRIVATE-HOST-FIXTURE" not in result.stderr
    assert "SECOND-PRIVATE-FIXTURE" not in result.stderr


def test_scanner_failure_cannot_be_bypassed_or_move_snapshot(repo: Repo) -> None:
    scanner(repo).write_text("#!/bin/sh\nexit 7\n")
    before = repo.git("rev-parse", "github-main")
    for arguments in (("--dry-run",), ("--allow-references",)):
        result = repo.run("github_snapshot.sh", *arguments)
        assert result.returncode == 1 and "gitleaks rejected" in result.stderr
    assert repo.git("rev-parse", "github-main") == before


@pytest.mark.skipif(SCANNER is None, reason="real gitleaks is required for secret detection")
def test_real_scanner_rejects_synthetic_key_even_with_archive_exclusion(repo: Repo) -> None:
    assert SCANNER
    scanner(repo).unlink()
    scanner(repo).symlink_to(SCANNER)
    clean = repo.run("github_snapshot.sh", "--dry-run")
    assert clean.returncode == 0, clean.stderr
    # Invented and nonfunctional; assemble at runtime so this test is safe to publish.
    token = "gh" + "p_" + "aB3dE6gH9jK2mN5pQ8sT1vW4xY7zC0fD2gH5"
    repo.commit(
        "synthetic credential fixture",
        {"fixture.txt": token, ".gitattributes": "fixture.txt export-ignore\n"},
    )
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "gitleaks rejected" in result.stderr
    assert "leaks found" in result.stderr
    assert token not in result.stdout + result.stderr
    assert repo.git("rev-parse", "github-main") == before
    new = repo.git("rev-parse", "main")
    record_gate(repo, new)
    result = push(repo, f"refs/heads/github-main {new} refs/heads/main {before}\n")
    assert result.returncode == 1 and "gitleaks rejected" in result.stderr
    assert token not in result.stdout + result.stderr


def test_hook_checks_every_ref_and_leaves_private_remote_alone(repo: Repo) -> None:
    old = repo.git("rev-parse", "main")
    new = repo.git("rev-parse", "eklenti/apple")
    good = f"refs/heads/github-main {new} refs/heads/main {old}\n"
    bad = f"refs/heads/eklenti/private {new} refs/heads/other {old}\n"
    assert push(repo, good + bad).returncode == 1
    assert push(repo, bad, "private-mirror").returncode == 0
    assert push(repo, good.replace("refs/heads/main", "refs/tags/release")).returncode == 1
    assert push(repo, f"(delete) {'0' * 40} refs/heads/main {old}\n").returncode == 1
    assert not (Path(repo.env["HOME"]) / "scanner-args").exists()


def test_hook_scan_ranges_new_remote_and_failures(repo: Repo) -> None:
    old = repo.git("rev-parse", "main")
    new = repo.git("rev-parse", "eklenti/apple")
    update = f"refs/heads/github-main {new} refs/heads/main {old}\n"
    missing = push(repo, update)
    assert missing.returncode == 1 and "no CI gate receipt" in missing.stderr
    record_gate(repo, new)
    assert push(repo, update).returncode == 0
    assert push(repo, update.replace(old, "0" * 40)).returncode == 0
    args = (Path(repo.env["HOME"]) / "scanner-args").read_text()
    assert f"--log-opts={old}..{new}\n" in args
    assert f"--log-opts={new}\n" in args
    assert "--allow-leaks" not in args
    scanner(repo).write_text("#!/bin/sh\nexit 1\n")
    assert push(repo, update).returncode == 1
    result = push(repo, update.replace(old, "1" * 40))
    assert result.returncode == 1 and "fetch origin" in result.stderr


def test_make_hooks_installs_executable_template(repo: Repo) -> None:
    root = Path(__file__).resolve().parents[1]
    repo.write("Makefile", (root / "Makefile").read_text())
    # Source file's executable mode is part of the installation contract.
    mode = (root / "scripts/hooks/pre-push").stat().st_mode
    (repo.path / "scripts/hooks/pre-push").chmod(mode)
    result = subprocess.run(
        ["make", "hooks"], cwd=repo.path, env=repo.env, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert repo.git("config", "--local", "core.hooksPath") == "scripts/hooks"
    assert os.access(repo.path / "scripts/hooks/pre-push", os.X_OK)
