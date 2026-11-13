"""The host-side initializer never replaces existing configuration or reveals secrets."""

import ast
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.secret_strength import strong_secret
from scripts import init_env

SCRIPT = Path(init_env.__file__).resolve()
TEMPLATE = SCRIPT.parent.parent / ".env.example"


def run_init(template, output):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--template", str(template), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("mask", [0o000, 0o077, 0o777])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_generated_values_permissions_and_unchanged_lines(tmp_path, mask, ending):
    template = tmp_path / "template"
    original = TEMPLATE.read_bytes().replace(b"\n", ending) + b"# final comment without newline"
    template.write_bytes(original)
    output = tmp_path / "output"
    previous = os.umask(mask)
    try:
        result = run_init(template, output)
    finally:
        os.umask(previous)
    assert result.returncode == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    values = {}
    for before, after in zip(
        original.splitlines(keepends=True),
        output.read_bytes().splitlines(keepends=True),
        strict=True,
    ):
        key = before.decode().partition("=")[0]
        if key in init_env.KEYS:
            value = after.decode().strip().split("=", 1)[1]
            assert strong_secret(value)
            assert value not in result.stdout + result.stderr
            values[key] = value
        else:
            assert before == after
    assert len(set(values.values())) == 3
    assert len(values["APP_UI_TOKEN"]) == 43
    assert all(len(values[key]) == 64 for key in init_env.KEYS[1:])
    assert result.stdout == init_env.SUCCESS + "\n"
    assert result.stderr == ""


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_existing_output_is_untouched(tmp_path, kind):
    output = tmp_path / "output"
    target = tmp_path / "target"
    if kind == "file":
        output.write_bytes(b"keep existing configuration")
        output.chmod(0o640)
    elif kind == "directory":
        output.mkdir(mode=0o750)
    else:
        if kind == "symlink":
            target.write_bytes(b"keep target")
            target.chmod(0o640)
        output.symlink_to(target)
    before = output.lstat()
    result = run_init(tmp_path / "missing-template", output)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == init_env.EXISTS + "\n"
    assert output.lstat() == before
    if kind == "file":
        assert output.read_bytes() == b"keep existing configuration"
    if kind == "symlink":
        assert target.read_bytes() == b"keep target"
        assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.parametrize("key", init_env.KEYS)
@pytest.mark.parametrize("invalid", ["missing", "populated", "duplicate"])
def test_invalid_template_creates_nothing(tmp_path, key, invalid):
    original = TEMPLATE.read_text()
    replacement = {
        "missing": "",
        "populated": key + "=do-not-print",
        "duplicate": key + "=\n" + key + "=",
    }
    template = tmp_path / "template"
    template.write_text(original.replace(key + "=\n", replacement[invalid] + "\n"))
    output = tmp_path / "output"
    result = run_init(template, output)
    assert result.returncode == 1
    assert not output.exists()
    assert "do-not-print" not in result.stdout + result.stderr


def test_exclusive_create_protects_racing_writer(tmp_path):
    output = tmp_path / "output"
    content = init_env.render(TEMPLATE)
    output.write_bytes(b"other writer")
    with pytest.raises(FileExistsError):
        init_env.create(output, content)
    assert output.read_bytes() == b"other writer"


@pytest.mark.parametrize("failure", ["permissions", "write"])
def test_failed_write_removes_partial_file(tmp_path, monkeypatch, failure):
    def fail(*args):
        raise OSError("synthetic failure")

    output = tmp_path / "output"
    if failure == "permissions":
        monkeypatch.setattr(init_env.os, "fchmod", fail)
    else:
        real_fdopen = os.fdopen

        def read_only(fd, mode):
            return real_fdopen(fd, "rb")

        monkeypatch.setattr(init_env.os, "fdopen", read_only)
    with pytest.raises(OSError):
        init_env.create(output, b"synthetic content")
    assert not output.exists()


def test_stdlib_only_and_python39_syntax():
    tree = ast.parse(SCRIPT.read_text(), feature_version=(3, 9))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module.split(".")[0] in sys.stdlib_module_names


def test_make_env_defaults_and_second_run(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    shutil.copyfile(TEMPLATE, tmp_path / ".env.example")
    shutil.copyfile(SCRIPT.parent.parent / "Makefile", tmp_path / "Makefile")
    command = ["make", "-C", str(tmp_path), "env"]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    output = tmp_path / ".env"
    assert first.returncode == 0
    before = output.read_bytes(), output.stat()
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode == 2
    assert init_env.EXISTS in second.stderr
    assert (output.read_bytes(), output.stat()) == before
