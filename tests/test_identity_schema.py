"""Schema verification belongs to its defining module, independent of orchestration."""

import importlib
import subprocess
import sys

import pytest


def test_schema_import_does_not_load_identity_or_backfill() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services.identities import identity_schema; "
            "import sys; "
            "assert 'app.services.identities.identity' not in sys.modules; "
            "assert 'app.services.identities.identity_backfill' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "identity_tables_missing",
        "verify_identity_schema",
        "verify_views",
        "EXPECTED_CONSTRAINTS",
    ],
)
def test_schema_exports_have_no_legacy_facade_copy(name: str) -> None:
    schema = importlib.import_module("app.services.identities.identity_schema")
    legacy = importlib.import_module("app.services.identities.identity")
    exported = getattr(schema, name)

    assert not hasattr(legacy, name)
    if callable(exported):
        assert exported.__module__ == schema.__name__
