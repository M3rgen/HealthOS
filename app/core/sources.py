"""Source selection shared by startup and the deployment configuration verifier."""

import os
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import unquote, urlsplit

SOURCE_NAMES = ("google", "garmin", "apple")
# Sources whose importer ships separately: selecting one in a tree without it imports nothing.
OPTIONAL_IMPORTERS = {"apple": "app.apple"}


def parse_sources(value: str | list[str]) -> list[str]:
    items = value.split(",") if isinstance(value, str) else value
    sources = list(dict.fromkeys(item.strip().lower() for item in items))
    if sources == ["none"]:
        return []
    if not sources or any(source not in SOURCE_NAMES for source in sources):
        raise ValueError("HEALTHOS_SOURCES must list google, garmin, apple or only none")
    return sources


def selected_sources(value: str | list[str], *, legacy_garmin: bool = False) -> list[str]:
    sources = parse_sources(value)
    if legacy_garmin and "garmin" not in sources:
        sources.append("garmin")
    return sources


def not_installed_sources(selected: list[str]) -> list[str]:
    """The selected sources whose importer is not part of this installation."""
    return [
        source
        for source in selected
        if source in OPTIONAL_IMPORTERS and find_spec(OPTIONAL_IMPORTERS[source]) is None
    ]


def safe_database_url(value: str) -> bool:
    try:
        password = unquote(urlsplit(value).password or "").strip()
    except ValueError:
        return False
    return bool(password) and password.casefold() not in {
        "health",
        "postgres",
        "password",
        "change_me",
        "changeme",
        "todo",
        "replace_me",
        "example-secret",
    }


def readable_garmin_key(path: Path) -> bool:
    try:
        # Check readability without retaining or reporting the file's contents.
        with path.expanduser().open("rb") as key:
            return bool(key.read(1))
    except (OSError, ValueError):
        return False


def garmin_key_mode(path: Path) -> int | None:
    """Inspect permissions without opening or reading the key."""
    try:
        return os.stat(path.expanduser()).st_mode & 0o777
    except OSError:
        return None


def permissive_key_mode(mode: int | None) -> bool:
    return mode is not None and mode & 0o077 != 0
