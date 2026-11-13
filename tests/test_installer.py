"""Check the installed console script from outside the source directory."""

import os
import subprocess
import sysconfig
from pathlib import Path


def test_installed_cli_entry_point(tmp_path):
    command = Path(sysconfig.get_path("scripts")) / "healthos"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [str(command), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "sync" in result.stdout
