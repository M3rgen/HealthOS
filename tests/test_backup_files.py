"""Encrypted artifacts are published completely and never replace an existing name."""

import io
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.operations import backup_files


@pytest.mark.parametrize("method", ["age", "gpg"])
def test_private_allocation_and_concurrent_no_clobber_publication(tmp_path, method):
    target = tmp_path / f"health-fixed.dump.{method}"
    target.write_bytes(b"previous-success")
    partials = [backup_files.create_partial(target) for _ in range(2)]
    assert len(set(partials)) == 2
    for i, partial in enumerate(partials):
        assert stat.S_IMODE(partial.stat().st_mode) == 0o600
        partial.write_bytes(f"complete-ciphertext-{i}".encode())
    with target.open("rb") as original, ThreadPoolExecutor(2) as pool:
        outputs = list(pool.map(lambda p: backup_files.publish(p, target), partials))
        assert original.read() == b"previous-success"
    assert target.read_bytes() == b"previous-success"
    assert len(set(outputs)) == 2
    assert [p.read_bytes() for p in outputs] == [b"complete-ciphertext-0", b"complete-ciphertext-1"]
    assert all(p in tmp_path.glob(f"health-*.dump.{method}") for p in outputs)
    assert not any(p.exists() for p in partials)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_occupied_names_including_dangling_links_are_preserved(tmp_path, kind):
    target = tmp_path / "health-fixed.dump.age"
    if kind == "file":
        target.write_bytes(b"old")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    before = target.lstat()
    partial = backup_files.create_partial(target)
    partial.write_bytes(b"ciphertext")
    output = backup_files.publish(partial, target)
    assert output != target and output.read_bytes() == b"ciphertext"
    assert target.lstat() == before


@pytest.mark.parametrize("failure", ["empty", "link", "collisions"])
def test_failure_keeps_existing_artifact_and_only_callers_partial(tmp_path, monkeypatch, failure):
    target = tmp_path / "health-fixed.dump.age"
    target.write_bytes(b"old")
    partial = backup_files.create_partial(target)
    peer = backup_files.create_partial(target)
    if failure != "empty":
        partial.write_bytes(b"ciphertext")

        def refused(*args):
            raise FileExistsError if failure == "collisions" else OSError("link unavailable")

        monkeypatch.setattr(backup_files.os, "link", refused)
    with pytest.raises((ValueError, OSError)):
        backup_files.publish(partial, target)
    assert target.read_bytes() == b"old"
    assert partial.exists() and peer.exists()
    assert set(tmp_path.iterdir()) == {target, partial, peer}


@pytest.mark.parametrize("name", ["backup.sql", ".dump.age", "backup.dump.zip"])
def test_publish_rejects_target_without_encrypted_dump_suffix(tmp_path, name):
    partial = tmp_path / "piece.partial"
    partial.write_bytes(b"ciphertext")
    with pytest.raises(ValueError, match="encrypted dump target required"):
        backup_files.publish(partial, tmp_path / name)
    assert set(tmp_path.iterdir()) == {partial}


def test_publish_rejects_partial_outside_backup_directory_or_equal_to_target(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    outside = elsewhere / "piece.partial"
    outside.write_bytes(b"ciphertext")
    target = backups / "night.dump.age"
    with pytest.raises(ValueError, match="separate temporary file"):
        backup_files.publish(outside, target)
    target.write_bytes(b"ciphertext")
    with pytest.raises(ValueError, match="separate temporary file"):
        backup_files.publish(target, target)
    assert target.read_bytes() == b"ciphertext" and outside.exists()


class _Stdio:
    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)


def test_main_check_stream_forwards_bytes_and_fails_closed_on_empty(monkeypatch, capsys):
    out = _Stdio()
    monkeypatch.setattr(backup_files.sys, "stdin", _Stdio(b"dump-bytes"))
    monkeypatch.setattr(backup_files.sys, "stdout", out)
    assert backup_files.main(["check-stream"]) == 0
    assert out.buffer.getvalue() == b"dump-bytes"

    monkeypatch.undo()
    monkeypatch.setattr(backup_files.sys, "stdin", _Stdio(b""))
    assert backup_files.main(["check-stream"]) == 1
    assert capsys.readouterr().err == "backup: pg_dump stream is empty or could not be forwarded\n"


def test_main_create_then_publish_prints_paths(tmp_path, capsys):
    target = tmp_path / "night.dump.gpg"
    assert backup_files.main(["create", str(target)]) == 0
    partial = Path(capsys.readouterr().out.strip())
    assert partial.parent == tmp_path and partial.name.endswith(".partial")
    assert stat.S_IMODE(partial.stat().st_mode) == 0o600
    partial.write_bytes(b"ciphertext")

    assert backup_files.main(["publish", str(partial), str(target)]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert target.read_bytes() == b"ciphertext" and not partial.exists()


@pytest.mark.parametrize(
    "args", [[], ["create"], ["publish", "only-one"], ["remove", "x"], ["check-stream", "extra"]]
)
def test_main_rejects_unknown_operations_without_output(args, capsys):
    assert backup_files.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "backup: could not allocate or publish encrypted dump\n"


def test_main_reports_publish_failure_and_keeps_partial(tmp_path, capsys):
    partial = tmp_path / "piece.partial"
    partial.write_bytes(b"")
    target = tmp_path / "night.dump.age"
    assert backup_files.main(["publish", str(partial), str(target)]) == 1
    assert capsys.readouterr().err == "backup: could not allocate or publish encrypted dump\n"
    assert partial.exists() and not target.exists()
