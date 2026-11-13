"""Check maintainer-file exposure through the runtime stage without building Docker."""

import fnmatch
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAINTAINER_PATTERNS = (
    "scripts/ci_gate.sh",
    "scripts/release_gate.py",
    "scripts/check_no_extension.sh",
    "scripts/github_snapshot*",
    "scripts/hooks/",
    "**/*.ps1",
)


def test_runtime_copy_excludes_maintainer_files():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    sources = []
    for line in (ROOT / "Dockerfile").read_text().replace("\\\n", " ").splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0].upper() == "FROM":
            sources = []
        if words and words[0].upper() == "COPY" and not words[1].startswith("--from="):
            sources.extend(words[1:-1])

    def copied(path):
        return any(
            source == "." or path == source or path.startswith(source.rstrip("/") + "/")
            for source in sources
        )

    def ignored(path):
        excluded = False
        parents = [path, *(parent.as_posix() for parent in Path(path).parents)]
        for pattern in patterns:
            if not pattern or pattern.startswith("#"):
                continue
            rule = pattern.removeprefix("!").strip("/")
            variants = (rule, rule.removeprefix("**/"))
            if any(fnmatch.fnmatchcase(part, variant) for part in parents for variant in variants):
                excluded = not pattern.startswith("!")
        return excluded

    candidates = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "scripts").rglob("*")
        if path.is_file()
    }
    # Cover future PowerShell files at the root and inside other copied directories too.
    candidates.update(("maintainer.ps1", "app/maintainer.ps1", "scripts/hooks/nested/check.ps1"))
    maintainers = {
        path
        for path in candidates
        if any(
            fnmatch.fnmatchcase(path, pattern.rstrip("/") + "*") for pattern in MAINTAINER_PATTERNS
        )
        or path.endswith(".ps1")
    }
    exposed = sorted(path for path in maintainers if copied(path) and not ignored(path))
    assert not exposed, f"Maintainer files in runtime image: {exposed}"
    assert set(MAINTAINER_PATTERNS) <= set(patterns)
    assert copied("scripts/docker-entrypoint.sh") and not ignored("scripts/docker-entrypoint.sh")
    assert copied("scripts/backup.sh") and not ignored("scripts/backup.sh")
