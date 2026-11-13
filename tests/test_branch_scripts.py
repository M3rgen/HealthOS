"""scripts/github_snapshot.sh and scripts/check_no_extension.sh against throwaway git repos."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(GIT is None or BASH is None, reason="git and bash are required")

MIGRATION = 'revision = "{rev}"\ndown_revision = {down}\n\n\ndef upgrade():\n    pass\n'
RELEASE = "Unreleased\n\n### Fixed\n\n- Preserve every source record.\n  Keep its history."


class Repo:
    def __init__(self, path: Path, home: Path) -> None:
        self.path = path
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "TMPDIR": str(home),
            "LANG": "C",
        }

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message: str, files: dict[str, str]) -> None:
        for path, text in files.items():
            self.write(path, text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", f"scripts/{script}", *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def files(self, rev: str) -> set[str]:
        return set(self.git("ls-tree", "-r", "--name-only", rev).splitlines())


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = home / "bin"
    bin_dir.mkdir()
    scanner = bin_dir / "gitleaks"
    scanner.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$HOME/scanner-args"\nexit 0\n')
    scanner.chmod(0o755)
    repo = Repo(tmp_path / "repo", home)
    repo.env["PATH"] = f"{bin_dir}:{repo.env['PATH']}"
    repo.path.mkdir()
    repo.git("-c", "init.defaultBranch=main", "init", "-q")
    for key, value in {
        "user.name": "Test",
        "user.email": "test@example.invalid",
        "commit.gpgsign": "false",
        "healthos.githubAuthorName": "Snapshot",
        "healthos.githubAuthorEmail": "snapshot@example.invalid",
        "healthos.githubExclude": "notes/maintainer-log.md",
        "healthos.extensionMarker": "secretword",
        "healthos.githubExtension": "eklenti/apple",
        "healthos.githubExtensionMarker": "applehealth",
        "healthos.githubForbidPattern": "private-host-fixture",
    }.items():
        repo.git("config", key, value)
    for script in (
        "github_snapshot.sh",
        "github_snapshot_checks.py",
        "check_no_extension.sh",
        "hooks/pre-push",
    ):
        repo.write(f"scripts/{script}", (ROOT / "scripts" / script).read_text(encoding="utf-8"))
    repo.commit(
        "base",
        {
            "README.md": "HealthOS\n",
            "CHANGELOG.md": f"# Changelog\n\n## {RELEASE}\n\n## 0.1.0\n\n- Older release.\n",
            "docs/BRANCHES.md": "private-host-fixture\n",
            "app/core.py": "VALUE = 1\n",
            "notes/maintainer-log.md": "maintainer notes\n",
            "migrations/versions/m0001.py": MIGRATION.format(rev="0001", down="None"),
        },
    )
    repo.git("branch", "github-main")
    repo.git("branch", "dev")
    repo.git("switch", "-q", "-c", "eklenti/apple")
    repo.commit(
        "apple",
        {
            "app/apple_import.py": "APPLEHEALTH_ENABLED = False\n",
            "migrations/versions/m0002_apple.py": MIGRATION.format(rev="0002a", down='"0001"'),
        },
    )
    repo.git("switch", "-q", "-c", "eklenti/x", "main")
    repo.commit("private", {"app/private_ext.py": "SECRETWORD = 1\n"})
    repo.git("switch", "-q", "main")
    return repo


def test_snapshot_merges_github_only_extension(repo: Repo) -> None:
    result = repo.run("github_snapshot.sh")
    assert result.returncode == 0, result.stderr
    assert "merged: eklenti/apple @" in result.stdout
    files = repo.files("github-main")
    assert "app/apple_import.py" in files
    assert "migrations/versions/m0002_apple.py" in files
    assert "notes/maintainer-log.md" not in files
    assert "app/private_ext.py" not in files
    message = repo.git("log", "-1", "--format=%B", "github-main")
    assert message == f"{RELEASE}\nSource: dev {repo.git('rev-parse', '--short=7', 'main')}"
    assert "docs/BRANCHES.md" not in files
    assert repo.git("rev-list", "--count", "github-main") == "2"


def test_snapshot_without_extensions(repo: Repo) -> None:
    result = repo.run("github_snapshot.sh", "--no-extensions")
    assert result.returncode == 0, result.stderr
    assert "app/apple_import.py" not in repo.files("github-main")


def test_snapshot_dry_run_does_not_move_branch(repo: Repo) -> None:
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry run: tree" in result.stdout
    assert repo.git("rev-parse", "github-main") == before


def test_snapshot_conflict_exits_2(repo: Repo) -> None:
    repo.commit("main change", {"app/core.py": "VALUE = 2\n"})
    repo.git("switch", "-q", "eklenti/apple")
    repo.commit("apple change", {"app/core.py": "VALUE = 3\n"})
    repo.git("switch", "-q", "main")
    before = repo.git("rev-parse", "github-main")
    result = repo.run("github_snapshot.sh")
    assert result.returncode == 2
    assert "merge main into eklenti/apple first" in result.stderr
    assert "conflict: app/core.py" in result.stderr
    assert repo.git("rev-parse", "github-main") == before


def test_snapshot_missing_extension_branch_exits_2(repo: Repo) -> None:
    repo.git("config", "--add", "healthos.githubExtension", "eklenti/missing")
    result = repo.run("github_snapshot.sh", "--dry-run")
    assert result.returncode == 2
    assert "eklenti/missing" in result.stderr


def test_snapshot_refuses_two_alembic_heads(repo: Repo) -> None:
    repo.commit(
        "main migration",
        {"migrations/versions/m0002.py": MIGRATION.format(rev="0002", down='"0001"')},
    )
    before = repo.git("rev-parse", "github-main")
    for args in (("--dry-run",), ()):
        result = repo.run("github_snapshot.sh", *args)
        assert result.returncode == 2, result.stdout
        assert "alembic head: 0002 " in result.stderr
        assert "alembic head: 0002a " in result.stderr
    assert repo.git("rev-parse", "github-main") == before

    repo.git("switch", "-q", "eklenti/apple")
    repo.git("merge", "-q", "--no-edit", "main")
    merge = 'revision = "0003m"\ndown_revision = (\n    "0002a",\n    "0002",\n)\n'
    repo.commit("merge heads", {"migrations/versions/m0003_merge.py": merge})
    repo.git("switch", "-q", "main")
    result = repo.run("github_snapshot.sh")
    assert result.returncode == 0, result.stderr
    assert "migrations/versions/m0003_merge.py" in repo.files("github-main")


def test_check_github_only_file_leaked_into_main(repo: Repo) -> None:
    assert repo.run("check_no_extension.sh").returncode == 0
    repo.commit("leak", {"app/apple_import.py": "copied\n"})
    result = repo.run("check_no_extension.sh", "main")
    assert result.returncode == 1
    assert "main:app/apple_import.py (added by eklenti/apple, GitHub-only)" in result.stdout


def test_check_github_only_marker_in_main(repo: Repo) -> None:
    repo.commit("leak", {"app/other.py": "if settings.applehealth_enabled: pass\n"})
    result = repo.run("check_no_extension.sh", "main")
    assert result.returncode == 1
    assert "main:app/other.py" in result.stdout


def test_check_snapshot_allows_github_only_but_not_private(repo: Repo) -> None:
    assert repo.run("github_snapshot.sh").returncode == 0
    result = repo.run("check_no_extension.sh", "github-main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.run("check_no_extension.sh", "eklenti/apple").returncode == 0

    repo.git("switch", "-q", "github-main")
    repo.commit("private leak", {"app/leak.py": "secretword\n"})
    repo.git("switch", "-q", "main")
    result = repo.run("check_no_extension.sh", "github-main")
    assert result.returncode == 1
    assert "github-main:app/leak.py" in result.stdout


def test_check_github_only_mode_on_deployment(repo: Repo) -> None:
    repo.git("switch", "-q", "-c", "deployed")
    repo.git("merge", "-q", "--no-ff", "--no-edit", "eklenti/x")
    result = repo.run("check_no_extension.sh", "--github-only", "deployed")
    assert result.returncode == 0, result.stdout + result.stderr
    assert repo.run("check_no_extension.sh", "deployed").returncode == 1

    repo.git("merge", "-q", "--no-ff", "--no-edit", "eklenti/apple")
    result = repo.run("check_no_extension.sh", "--github-only", "deployed")
    assert result.returncode == 1
    assert "deployed:app/apple_import.py (added by eklenti/apple, GitHub-only)" in result.stdout


def test_check_github_only_mode_needs_no_private_marker(repo: Repo) -> None:
    repo.git("config", "--unset-all", "healthos.extensionMarker")
    assert repo.run("check_no_extension.sh").returncode == 2
    assert repo.run("check_no_extension.sh", "--github-only").returncode == 0


def test_check_unconfigured_exits_2(repo: Repo) -> None:
    repo.git("config", "--unset-all", "healthos.githubExtension")
    repo.git("config", "--unset-all", "healthos.githubExtensionMarker")
    result = repo.run("check_no_extension.sh", "--github-only", "main")
    assert result.returncode == 2
    assert "refusing to report clean" in result.stderr
    repo.git("config", "--unset-all", "healthos.extensionMarker")
    assert repo.run("check_no_extension.sh").returncode == 2
