"""Host-side Python helpers leave their source tree untouched on success and failure."""

import json
from pathlib import Path

import pytest
from support.backup_scripts import (
    fake_age as fake_age,
)
from support.backup_scripts import (
    run_with_env_file,
    status,
)
from support.backup_scripts import (
    sandbox as sandbox,
)


@pytest.mark.parametrize("failed", [False, True])
def test_backup_helpers_do_not_write_bytecode_in_the_source_tree(tmp_path, fake_age, failed):
    env = {**fake_age, "PYTHONDONTWRITEBYTECODE": ""}
    if failed:
        env["FAKE_DOCKER_FAIL"] = "1"
    result = run_with_env_file(tmp_path, env, "")
    assert result.returncode == int(failed), result.stderr
    assert "could not publish status metadata" not in result.stderr
    expected = "failure" if failed else "success"
    assert status(env)["result"] == expected
    metadata = json.loads((Path(env["BACKUP_STATUS_HOST_DIR"]) / "status.json").read_text())
    assert metadata["last_attempt"]["result"] == expected
    assert not list((tmp_path / "tree").rglob("__pycache__"))
    assert not list((tmp_path / "tree").rglob("*.pyc"))
