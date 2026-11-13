"""Check migration coverage and inline repository paths without a build or database."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = re.compile(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[ \t]*$", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
REPOSITORY_PATH = re.compile(
    r"(?<![\w/])(?:app|scripts|tests|frontend|deploy|migrations)/[^\s`<>\[\]()\"',;]+"
)
# Exact paths only; missing descendants must still fail. None is shipped in a clean checkout.
NON_CHECKOUT_PATHS = {
    "app/static": "Vite build output, gitignored",
    "frontend/.test-output": "Browser screenshots, generated and gitignored",
    "frontend/.test-output/dist": "Browser build output, generated and gitignored",
    "app/apple": "Release-only extension, explicitly absent in docs/reference/sources.md",
    "deploy/cloudflare/*.json": "Private tunnel credentials, explicitly excluded by .gitignore",
}


def missing_migration_notes(root: Path = ROOT) -> list[str]:
    """Read revision declarations, never import or execute migration code."""
    notes = (root / "docs/MIGRATIONS.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for migration in sorted((root / "migrations/versions").glob("*.py")):
        for revision in REVISION.findall(migration.read_text(encoding="utf-8")):
            if revision not in notes:
                missing.append(revision)
    return missing


def inline_repository_paths(markdown: str) -> list[tuple[int, str]]:
    """Extract paths even inside commands; preserve line numbers when removing fences.

    A glob must match at least one file or directory. Template placeholders terminate the
    literal path, so examples such as tests/browser/<name>.cjs still check their parent.
    """
    text = FENCE.sub(lambda match: "\n" * match[0].count("\n"), markdown)
    paths: list[tuple[int, str]] = []
    for span in CODE_SPAN.finditer(text):
        for match in REPOSITORY_PATH.finditer(span[2]):
            number = text.count("\n", 0, span.start(2) + match.start()) + 1
            paths.append((number, match[0].rstrip(".:/")))
    return paths


def missing_repository_paths(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    for page in sorted((root / "docs").rglob("*.md")):
        for number, path in inline_repository_paths(page.read_text(encoding="utf-8")):
            if path in NON_CHECKOUT_PATHS:
                continue
            if not any(match.is_file() or match.is_dir() for match in root.glob(path)):
                problems.append(f"{page.relative_to(root).as_posix()}:{number}: {path}")
    return problems


def main() -> int:
    problems = [
        *(f"Missing migration note: {revision}" for revision in missing_migration_notes()),
        *missing_repository_paths(),
    ]
    for problem in problems:
        print(problem)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
