"""Synthetic input checks for the standalone documentation verifier."""

from scripts.check_documentation import (
    inline_repository_paths,
    missing_migration_notes,
    missing_repository_paths,
)


def test_revision_comes_from_declaration_not_filename_or_parent(tmp_path):
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    (versions / "arbitrary_name.py").write_text(
        "revision = 'new_revision'\ndown_revision = 'old_revision'\nraise RuntimeError()\n"
    )
    (tmp_path / "docs").mkdir()
    notes = tmp_path / "docs/MIGRATIONS.md"
    notes.write_text("## old_revision\n")
    assert missing_migration_notes(tmp_path) == ["new_revision"]
    notes.write_text("## new_revision\n")
    assert missing_migration_notes(tmp_path) == []


def test_inline_paths_in_commands_globs_and_nested_docs(tmp_path):
    (tmp_path / "docs/nested").mkdir(parents=True)
    (tmp_path / "app/models").mkdir(parents=True)
    (tmp_path / "app/models/example.py").touch()
    page = tmp_path / "docs/nested/paths.md"
    page.write_text(
        "`app/models` and ``app/models/example.py``\n"
        "`python app/models/example.py` and `app/models/*.py`\n"
        "`app/services/storage.py` and `app/models/missing*.py`\n"
        "`app/static` and `app/static/missing.py`\n"
        "```sh\napp/example-only.py\n```\n"
        "Unquoted app/prose.py and external `https://example.com/app/url.py`\n"
    )
    assert missing_repository_paths(tmp_path) == [
        "docs/nested/paths.md:3: app/services/storage.py",
        "docs/nested/paths.md:3: app/models/missing*.py",
        "docs/nested/paths.md:4: app/static/missing.py",
    ]


def test_fences_and_multiline_spans_preserve_path_line_numbers():
    assert inline_repository_paths(
        "~~~sh\napp/ignored.py\n~~~\n\n`python\n scripts/check.py`\n`tests/browser/<name>.cjs`\n"
    ) == [(6, "scripts/check.py"), (7, "tests/browser")]
