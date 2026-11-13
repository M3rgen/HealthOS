"""Snapshot source traces, checked offline in disposable local repositories."""

import pytest
from test_branch_scripts import RELEASE, Repo
from test_branch_scripts import repo as repo


def displayed_message(stdout: str) -> bytes:
    return stdout.split("commit message:\n", 1)[1].split("\ndiff ", 1)[0].encode("utf-8")


@pytest.mark.parametrize("supplied", [None, "  Yeni sürüm  \n\nAçıklama.\n\n"])
def test_missing_trace_is_appended_without_rewriting_message(repo: Repo, supplied: str | None):
    arguments = ("-m", supplied) if supplied is not None else ()
    result = repo.run("github_snapshot.sh", "--dry-run", *arguments)
    assert result.returncode == 0, result.stderr
    message = RELEASE if supplied is None else supplied
    separator = "" if message.endswith("\n") else "\n"
    source = repo.git("rev-parse", "--short=7", "main")
    assert displayed_message(result.stdout) == f"{message}{separator}Source: dev {source}".encode()


@pytest.mark.parametrize(
    "template",
    ["Release dev {sha}", "  Sürüm  \n\nSource: dev {sha}\n\n", "dev {sha}\nSource: dev {sha}"],
)
def test_matching_trace_preserves_message_bytes(repo: Repo, template: str):
    message = template.format(sha=repo.git("rev-parse", "--short=7", "main"))
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", message)
    assert result.returncode == 0, result.stderr
    assert displayed_message(result.stdout) == message.encode("utf-8")


@pytest.mark.parametrize("matching_first", [False, True])
def test_wrong_trace_is_rejected_even_alongside_correct_trace(repo: Repo, matching_first: bool):
    source = repo.git("rev-parse", "--short=7", "main")
    wrong = repo.git("rev-parse", "--short=7", "eklenti/apple")
    assert wrong != source
    message = f"Release dev {wrong}"
    if matching_first:
        message = f"Release dev {source}\n{message}"
    before = repo.git("rev-parse", "github-main"), repo.git("write-tree")
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", message)
    assert result.returncode == 1
    assert "dev trace does not match the source commit" in result.stderr
    assert before == (repo.git("rev-parse", "github-main"), repo.git("write-tree"))


@pytest.mark.parametrize("source", ["dev", "dev~1"])
def test_trace_resolves_selected_source_commit(repo: Repo, source: str):
    repo.git("switch", "-q", "dev")
    repo.commit("first source", {"app/selected.py": "VALUE = 1\n"})
    repo.commit("second source", {"app/selected.py": "VALUE = 2\n"})
    repo.git("switch", "-q", "main")
    result = repo.run("github_snapshot.sh", "--dry-run", "--source", source)
    assert result.returncode == 0, result.stderr
    short = displayed_message(result.stdout).decode().split("Source: dev ")[1]
    assert len(short) == 7
    assert repo.git("rev-parse", short) == repo.git("rev-parse", source)
    assert repo.git("rev-parse", short) != repo.git("rev-parse", "HEAD")


@pytest.mark.parametrize("private", ["eklenti/private", "abc1234"])
def test_matching_trace_does_not_bypass_other_message_checks(repo: Repo, private: str):
    source = repo.git("rev-parse", "--short=7", "main")
    result = repo.run("github_snapshot.sh", "--dry-run", "-m", f"dev {source}\nRelease {private}")
    assert result.returncode == 1
    assert "private branch name or a hash-shaped word" in result.stderr
