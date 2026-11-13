"""The running version, taken from the installed project metadata (pyproject.toml is the source)."""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION = "healthos"


def app_version() -> str:
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return "0+unknown"
