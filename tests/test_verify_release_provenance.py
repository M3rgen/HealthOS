"""Consumer provenance verification uses gh without requiring network access in tests."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_release_provenance.sh"
BASH = shutil.which("bash")


@pytest.fixture
def cli(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    return {
        "PATH": str(binaries),
        "ARGS_LOG": str(tmp_path / "gh-args"),
        "EXIT_CODE": "0",
    }


def run(cli, *args):
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=cli, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("exit_code", [0, 1, 37, 127])
@pytest.mark.parametrize("repo", [None, "example/release-fork"])
@pytest.mark.parametrize("asset", ["healthos-image.tar.gz", "healthos.spdx.json"])
def test_arguments_and_exit_status(cli, tmp_path, exit_code, repo, asset):
    gh = Path(cli["PATH"]) / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_LOG"\nexit "$EXIT_CODE"\n')
    gh.chmod(0o755)
    cli["EXIT_CODE"] = str(exit_code)
    directory = tmp_path / "downloaded release"
    directory.mkdir()
    archive = directory / asset
    archive.write_bytes(b"synthetic release asset")

    result = run(cli, str(archive), *(["--repo", repo] if repo else []))

    assert result.returncode == exit_code, result.stderr
    assert Path(cli["ARGS_LOG"]).read_text().splitlines() == [
        "attestation",
        "verify",
        str(archive),
        "--repo",
        repo or "M3rgen/HealthOS",
    ]


def test_missing_gh_returns_two_with_clear_message(cli):
    assert shutil.which("gh", path=cli["PATH"]) is None
    result = run(cli, "healthos-image.tar.gz")
    assert result.returncode == 2
    assert "GitHub CLI (gh)" in result.stderr and "PATH" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("",),
        ("asset", "--repo"),
        ("asset", "--repo", ""),
        ("asset", "--other", "example/repo"),
        ("asset", "--repo", "example/repo", "extra"),
    ],
)
def test_invalid_usage_returns_two(cli, args):
    result = run(cli, *args)
    assert result.returncode == 2
    assert "Kullanım:" in result.stderr
