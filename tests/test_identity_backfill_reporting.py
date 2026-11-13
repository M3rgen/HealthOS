"""The shared reporting interface imports independently of the backfill executor."""

import importlib
import subprocess
import sys

import pytest


def test_reporting_import_does_not_load_the_backfill_executor() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.services.identities import identity_backfill_reporting as reporting; "
            "from app.services.identities.identity_backfill_stats import BackfillStats; "
            "import sys; "
            "assert 'app.services.identities.identity_backfill' not in sys.modules; "
            "assert reporting.BackfillStats is BackfillStats",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["Timeline", "together", "full", "report"])
def test_shared_reporting_helpers_belong_to_the_reporting_module(name: str) -> None:
    reporting = importlib.import_module("app.services.identities.identity_backfill_reporting")
    helper = getattr(reporting, name)

    assert helper.__module__ == reporting.__name__
