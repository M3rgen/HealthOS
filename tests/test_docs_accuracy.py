"""Recurring documentation errors that are cheap to prevent."""

import re
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from app.core.config import Settings
from scripts.check_documentation import missing_migration_notes, missing_repository_paths
from scripts.verify_configuration import parse_env

ROOT = Path(__file__).resolve().parents[1]


def test_bridge_runbook_uses_readiness_notification():
    text = (ROOT / "docs/RUNBOOK.md").read_text(encoding="utf-8")
    section = text.split("## Restart the Umay bridge\n", 1)[1].split("\n## ", 1)[0]
    assert "Type=simple" not in section
    assert "Type=notify" in section
    assert "READY=1" in section


@pytest.mark.parametrize("name", ["docs/RUNBOOK.md", "docs/DEPLOYMENT.md"])
def test_bridge_login_uses_device_auth_and_service_home(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    command = (
        "sudo -u healthos-bridge env HOME=/var/lib/healthos-bridge "
        "CODEX_HOME=/var/lib/healthos-bridge /usr/local/bin/codex login "
        "-c cli_auth_credentials_store=file"
    )
    lines = {line.strip() for line in text.splitlines()}
    assert f"{command} --device-auth" in lines
    assert f"{command} status" in lines


def test_bridge_uv_installation_refers_to_project_requirement():
    text = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    section = text.split("## 9. Optional: the Umay bridge\n", 1)[1].split("\n## ", 1)[0]
    assert "required-version" in section
    assert "[tool.uv]" in section
    assert "[pyproject.toml](../pyproject.toml)" in section


def test_publication_hook_installation_is_only_in_maintainer_guide():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    development = (ROOT / "docs/DEVELOPMENT.md").read_text(encoding="utf-8")
    publication = development.split("## Continuous integration and releases\n", 1)[1]
    assert "make hooks" not in contributing
    assert "make hooks" in publication
    assert "only for the maintainer clone that publishes `github-main` to GitHub" in publication
    assert "Pushes to `origin` accept only\n`github-main:main` or a release tag" in publication


@pytest.mark.parametrize("name", ["README.md", "docker-compose.garmin.yml"])
def test_garmin_key_check_needs_no_host_account(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert "sudo -u '#10001'" not in text
    assert "sudo setpriv --reuid=10001 --regid=10001 --clear-groups test -r " in text
    assert "docker compose run --rm app healthos garmin-status" in text
    assert '"key_file_present": true' in text


def test_security_policy_links_to_private_vulnerability_reporting():
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    reporting = text.split("## Reporting a vulnerability\n", 1)[1].split("\n## ", 1)[0]
    assert "https://github.com/M3rgen/HealthOS/security/advisories/new" in reporting


@pytest.mark.parametrize(
    "name", ["README.md", "CONTRIBUTING.md", "docs/DEVELOPMENT.md", "docs/DEPLOYMENT.md"]
)
def test_guides_keep_dated_status_in_changelog(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    stale_status = re.compile(
        r"\b(?:on|as\s+of|since)\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}\b|\bNot\s+yet\s+proven\b",
        re.IGNORECASE,
    )
    matches = [
        (text.count("\n", 0, match.start()) + 1, match.group())
        for match in stale_status.finditer(text)
    ]
    assert matches == [], f"{name}: move dated status to CHANGELOG.md: {matches}"


# Letters that occur in Turkish words but not in English ones.
TURKISH_LETTERS = re.compile(r"[ğşıİĞŞ]")
MAX_TURKISH_LINES = 8
TURKISH_FILES = ("app/personas/",)  # The persona's own text is Turkish by design.


def turkish_prose_lines(text: str) -> list[int]:
    """Line numbers with Turkish outside code, quotes and bold UI strings, and outside a
    deliberately labelled Turkish section."""
    lines: list[int] = []
    fenced = in_turkish_section = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("#"):
            in_turkish_section = "Türkçe" in line
            continue
        if in_turkish_section:
            continue
        prose = re.sub(r"`[^`]*`|\"[^\"]*\"|\*\*[^*]*\*\*", "", line)
        if TURKISH_LETTERS.search(prose):
            lines.append(number)
    return lines


# Directory patterns from .gitignore, kept explicit so archives need no Git metadata.
MARKDOWN_EXCLUDED_DIRS = (
    ".git",
    ".venv",
    "node_modules",
    ".pytest_cache",
    "backups",
    "backup-status",
    "reports",
    "sast",
    "secrets",
    ".docker-public",
    ".test-tmp*",
    ".compose-smoke-*",
    ".uv-tool",
    ".python",
    ".uv-cache",
    ".venv-runtime-test",
    "__pycache__",
    ".ruff_cache",
    "dist",
    "*.egg-info",
    "htmlcov",
)
MARKDOWN_EXCLUDED_PATHS = (
    "deploy/cloudflare/credentials",
    "docs/design",
    "frontend/.test-output",
    "app/static",
)


def tracked_markdown(root: Path = ROOT) -> list[str]:
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--", "*.md"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (FileNotFoundError, subprocess.CalledProcessError):
        listed = []
        for path in root.rglob("*.md"):
            relative = path.relative_to(root)
            if any(
                fnmatchcase(part, pattern)
                for part in relative.parts[:-1]
                for pattern in MARKDOWN_EXCLUDED_DIRS
            ) or any(
                relative.as_posix().startswith(f"{prefix}/") for prefix in MARKDOWN_EXCLUDED_PATHS
            ):
                continue
            if path.is_file():
                listed.append(relative.as_posix())
    return [
        name
        for name in listed
        if not name.endswith(".tr.md") and not name.startswith(TURKISH_FILES)
    ]


def test_turkish_prose_detection_ignores_literal_ui_strings():
    assert turkish_prose_lines('The **Cihazlarım** panel, `Eşitleme` and "Sağlık" stay.') == []
    assert turkish_prose_lines("Kapsama raporu tamamlandı, doğrulama bakımcıda.") == [1]
    assert turkish_prose_lines("## Türkçe özet\n\nBu bölüm bilerek Türkçedir.") == []


def test_documentation_outside_tr_md_files_is_english():
    problems = {
        name: found
        for name in tracked_markdown()
        if len(found := turkish_prose_lines((ROOT / name).read_text(encoding="utf-8")))
        > MAX_TURKISH_LINES
    }
    assert problems == {}, "Translate to English, or rename the file *.tr.md"


def test_every_setting_and_template_variable_is_documented():
    documented = (ROOT / "docs/reference/config.md").read_text(encoding="utf-8")
    tokens = set(re.findall(r"`([A-Z][A-Z0-9_]+\*?)`", documented))
    prefixes = tuple(token[:-1] for token in tokens if token.endswith("*"))
    names = {name.upper() for name in Settings.model_fields} | set(parse_env(ROOT / ".env.example"))
    missing = sorted(name for name in names if name not in tokens and not name.startswith(prefixes))
    assert missing == []


def test_every_openapi_path_is_documented(monkeypatch):
    from app import main

    monkeypatch.setenv("HEALTHOS_SOURCES", "google,garmin")
    settings = Settings(_env_file=None, app_env="test")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    paths = main.create_app().openapi()["paths"]
    documented = (ROOT / "docs/reference/api.md").read_text(encoding="utf-8")

    def normalize(path):
        return re.sub(r"\{[^}]+\}", "{}", path)

    mentioned = {normalize(path) for path in re.findall(r"`[A-Z]+ (/[^`?\s]+)", documented)}
    missing = sorted(path for path in paths if normalize(path) not in mentioned)
    assert missing == [], f"Undocumented OpenAPI paths ({len(paths)} total): {missing}"


def test_every_reconciliation_setting_is_documented_by_full_name():
    documented = (ROOT / "docs/reference/config.md").read_text(encoding="utf-8")
    tokens = set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", documented))
    names = {name.upper() for name in Settings.model_fields if name.startswith("reconciliation_")}
    assert names <= tokens, sorted(names - tokens)


def test_every_migration_revision_is_documented():
    assert missing_migration_notes(ROOT) == []


def test_inline_repository_paths_resolve():
    assert missing_repository_paths(ROOT) == []


def test_archive_import_recipe_uses_owner_role():
    text = (ROOT / "docs/RUNBOOK.md").read_text(encoding="utf-8")
    section = text.split("## Import payload versions from archive dumps\n", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "exec -T app healthos import-versions" not in section
    command = "docker compose run --rm --no-deps -T migrate healthos import-versions"
    for options in (" --dry-run", ""):
        assert f"{command} --from-db health_archive{options} </dev/null" in section


def test_architecture_does_not_place_sqlite_ownership_in_runtime():
    text = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    sentences = re.split(r"(?<=[.!?])\s+", text)
    assert not any(
        "app/sync/ownership.py" in sentence and "sqlite" in sentence.lower()
        for sentence in sentences
    )


def test_architecture_service_module_count_matches_implementations():
    text = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    counts = re.findall(r"\b(\d+)\s+implementation modules\b", text)
    actual = sum(path.stem != "__init__" for path in (ROOT / "app/services").glob("*/*.py"))
    assert all(int(count) == actual for count in counts), (counts, actual)


@pytest.mark.parametrize("error", [FileNotFoundError, subprocess.CalledProcessError(128, "git")])
def test_markdown_fallback_excludes_generated_directories(tmp_path, monkeypatch, error):
    for directory in (
        ".git",
        ".venv",
        "nested/node_modules",
        "nested/.venv",
        ".pytest_cache",
        "frontend/.test-output",
        "backups",
        "backup-status",
        "reports",
        "sast",
        "docs/design",
        "deploy/cloudflare/credentials",
        "app/static",
        "secrets",
        ".test-tmp-example",
        ".compose-smoke-example",
        "package.egg-info",
        ".uv-cache",
    ):
        path = tmp_path / directory / "generated.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")
    for name in ("README.md", "README.tr.md", "app/personas/umay.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("documentation", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert tracked_markdown(tmp_path) == ["README.md"]


def test_markdown_archive_matches_git_listing(tmp_path):
    repository, archive_root = tmp_path / "repository", tmp_path / "archive"
    repository.mkdir()
    archive_root.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)

    git("init")
    for name in ("README.md", "docs/guide.md", "README.tr.md", "app/personas/umay.md"):
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("documentation", encoding="utf-8")
    git("add", ".")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Archive fixture",
    )
    archive = tmp_path / "source.tar"
    git("archive", "HEAD", "-o", str(archive))
    subprocess.run(["tar", "-xf", str(archive), "-C", str(archive_root)], check=True)
    assert not (archive_root / ".git").exists()
    assert set(tracked_markdown(archive_root)) == set(tracked_markdown(repository))
    generated = archive_root / "nested/node_modules/generated.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("generated", encoding="utf-8")
    assert set(tracked_markdown(archive_root)) == set(tracked_markdown(repository))
