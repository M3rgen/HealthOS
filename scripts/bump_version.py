"""Advance the release version after dating the current changelog section."""

import argparse
import json
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "pyproject.toml",
    "uv.lock",
    "frontend/package.json",
    "frontend/package-lock.json",
    "CHANGELOG.md",
)
# GitHub-only installer extension: when the tree carries install.sh, the installers' version
# markers and the release tag they and their documents pin (`v<version>` in install URLs and
# `git clone --branch`) follow the same bump. A tree without the extension changes only FILES.
INSTALLER_FILES = (
    "install.sh",
    "install.ps1",
    "README.md",
    "docs/INSTALL.md",
    "docs/INSTALL.tr.md",
    "docs/AI_INSTALL.md",
)
INSTALLER_MARKERS = {
    "install.sh": r'^HEALTHOS_INSTALLER_VERSION="',
    "install.ps1": r'^\$HEALTHOS_INSTALLER_VERSION = "',
}
NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = rf"{NUMBER}\.{NUMBER}\.{NUMBER}(?:\.{NUMBER})?"


def version_parts(version: str) -> tuple[int, ...]:
    if not re.fullmatch(VERSION, version):
        raise ValueError("expected a version X.Y.Z or X.Y.Z.N")
    return tuple(map(int, version.split(".")))


def npm_version(version: str) -> str:
    """npm uses build metadata for Python's fourth release component."""
    parts = version_parts(version)
    if len(parts) == 4 and parts[3] == 0:
        raise ValueError("the interim release counter must be positive")
    base = ".".join(map(str, parts[:3]))
    return f"{base}+{parts[3]}" if len(parts) == 4 else base


def next_version(current: str, target: str | None) -> str:
    parts = version_parts(current)
    if target is not None:
        proposed = version_parts(target)
        if len(proposed) != 3 or proposed <= parts:
            raise ValueError("target must be a three-part version greater than the current version")
        return target
    counter = parts[3] + 1 if len(parts) == 4 else 1
    return ".".join(map(str, (*parts[:3], counter)))


def replace_version(text: str, prefix: str, old: str, new: str) -> str:
    pattern = re.compile(rf'({prefix}){re.escape(old)}("[ \t]*,?[ \t]*$)', re.MULTILINE)
    if len(list(pattern.finditer(text))) != 1:
        raise ValueError("expected exactly one matching version line")
    return pattern.sub(lambda match: match[1] + new + match[2], text)


def changelog_section(text: str, old: str, new: str) -> str:
    heading = re.search(r"^## .+$", text, re.MULTILINE)
    if heading is None:
        raise ValueError("missing first changelog section")
    section = re.fullmatch(
        rf"## \[{re.escape(old)}\] - (Unreleased|[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})", heading[0]
    )
    if section is None:
        raise ValueError("first changelog section must match the current version and have a date")
    if section[1] == "Unreleased":
        raise ValueError(f"the current section is unreleased; date it when you tag {old}")
    date.fromisoformat(section[1])
    return (
        text[: heading.start()]
        + f"## [{new}] - Unreleased\n\n- (no changes yet)\n\n"
        + text[heading.start() :]
    )


def installer_tags(text: str, old: str, new: str) -> str:
    """`v<old>` as a whole tag becomes `v<new>`; a line linking a published release is kept."""
    tag = re.compile(rf"(?<![\w.])v{re.escape(old)}(?![\w]|\.[0-9])")
    return "".join(
        line if "releases/tag/" in line else tag.sub(f"v{new}", line)
        for line in text.splitlines(keepends=True)
    )


def render_installer(contents: dict[str, str], old: str, new: str) -> dict[str, str]:
    """The installer extension's files, checked before anything is written."""
    result = {}
    for name in INSTALLER_FILES:
        text = contents[name]
        if name in INSTALLER_MARKERS:
            marker = re.search(rf'{INSTALLER_MARKERS[name]}([^"]*)"$', text, re.MULTILINE)
            if marker is None or marker[1] != old:
                raise ValueError(f"{name} installer version must match the project version")
            text = replace_version(text, INSTALLER_MARKERS[name], old, new)
        result[name] = installer_tags(text, old, new)
    return result


def render(contents: dict[str, str], target: str | None) -> tuple[str, str, dict[str, str]]:
    """Prepare all five files before the caller writes any of them."""
    old = tomllib.loads(contents["pyproject.toml"])["project"]["version"]
    new = next_version(old, target)
    result = {"CHANGELOG.md": changelog_section(contents["CHANGELOG.md"], old, new)}
    lock = tomllib.loads(contents["uv.lock"])
    entries = [item for item in lock["package"] if item["name"] == "healthos"]
    if len(entries) != 1 or entries[0]["version"] != old:
        raise ValueError("uv.lock must contain one healthos entry matching the project version")
    result["pyproject.toml"] = replace_version(
        contents["pyproject.toml"], r'^\[project\]\n(?:(?!\[).*\n)*?version[ \t]*=[ \t]*"', old, new
    )
    result["uv.lock"] = replace_version(
        contents["uv.lock"], r'^name = "healthos"\nversion = "', old, new
    )
    for name in ("frontend/package.json", "frontend/package-lock.json"):
        text = contents[name]
        manifest = json.loads(text)
        if manifest["version"] != npm_version(old):
            raise ValueError(f"{name} must match the npm project version")
        updated = replace_version(
            text, r'^  "version"[ \t]*:[ \t]*"', npm_version(old), npm_version(new)
        )
        if name.endswith("package-lock.json"):
            if manifest["packages"][""]["version"] != npm_version(old):
                raise ValueError(
                    "package-lock.json root package must match the npm project version"
                )
            updated = replace_version(
                updated,
                r'^    ""[ \t]*:[ \t]*\{\n(?:      .*\n)*?      "version"[ \t]*:[ \t]*"',
                npm_version(old),
                npm_version(new),
            )
        result[name] = updated
    if "install.sh" in contents:
        result.update(render_installer(contents, old, new))
    return old, new, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("interim")
    target = commands.add_parser("target")
    target.add_argument("version", metavar="X.Y.Z")
    args = parser.parse_args(argv)
    try:
        names = FILES + (INSTALLER_FILES if (ROOT / "install.sh").is_file() else ())
        contents = {name: (ROOT / name).read_text(encoding="utf-8") for name in names}
        old, new, updates = render(contents, args.version if args.mode == "target" else None)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    for name in names:
        if updates[name] != contents[name]:
            (ROOT / name).write_text(updates[name], encoding="utf-8")
    print(f"{old} -> {new} (npm {npm_version(new)}); updated five release files")
    if len(names) > len(FILES):
        print("installer extension: version markers and release-tag URLs follow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
