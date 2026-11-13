#!/usr/bin/env python3
"""Fail unless two images built from one commit have the same file tree.

  compare_images.py IMAGE_A IMAGE_B
  compare_images.py --write-manifest IMAGE OUT.json     record one image's tree
  compare_images.py --against-manifest IN.json IMAGE    compare an image with a recorded tree

The recorded form exists because the image CI smoke-tested is gone when a release is built: CI
keeps its manifest, and release.yml refuses a release image whose tree differs from it.

Compared: every path with its type, mode, owner, link target and contents. Ignored, because they
record when a build ran and nothing else: file modification times, the source-mtime field of .pyc
headers (bytes 8-12), uv's own install timestamp (``*.dist-info/uv_cache.json`` and its
``RECORD`` line) and the password-change day of ``etc/shadow``, which ``useradd`` takes from the
day of the build unless SOURCE_DATE_EPOCH is set (Compose builds do not set it). A .pyc body is
compared as the code it holds, because marshal writes the same code differently depending on
interpreter state (reference flags, frozenset order); this needs the image's Python minor, and a
.pyc for another one is compared as bytes. Standard library only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import subprocess
import sys
import tarfile
import types
from collections.abc import Iterable, Iterator
from typing import IO

UV_CACHE = "uv_cache.json"
SHADOW = ("etc/shadow", "etc/shadow-")
SHADOW_LAST_CHANGE = 2  # the third colon-separated field of a line
PYC_MTIME = (8, 12)
PYC_HEADER = 16


def canonical(value: object) -> object:
    """A form of a marshalled constant whose repr depends only on its meaning."""
    if isinstance(value, types.CodeType):
        return (
            "code",
            value.co_name,
            value.co_qualname,
            value.co_flags,
            value.co_code,
            value.co_firstlineno,
            value.co_argcount,
            value.co_posonlyargcount,
            value.co_kwonlyargcount,
            value.co_stacksize,
            value.co_varnames,
            value.co_names,
            value.co_freevars,
            value.co_cellvars,
            value.co_linetable,
            value.co_exceptiontable,
            canonical(value.co_consts),
        )
    if isinstance(value, tuple):
        return tuple(canonical(item) for item in value)
    if isinstance(value, frozenset):
        return ("frozenset", sorted(repr(canonical(item)) for item in value))
    return value


def pyc_body(data: bytes) -> bytes:
    """The bytes that identify a .pyc: its code if it can be read here, else its raw body."""
    if data[:4] == importlib.util.MAGIC_NUMBER:
        try:
            return repr(canonical(marshal.loads(data[PYC_HEADER:]))).encode()  # noqa: S302
        except (EOFError, TypeError, ValueError):
            pass
    return data[PYC_MTIME[1] :]


def without_password_change_day(data: bytes) -> bytes:
    """Shadow lines with the build-day field blanked; hashes and every other field are kept."""
    lines = []
    for line in data.split(b"\n"):
        fields = line.split(b":")
        if len(fields) > SHADOW_LAST_CHANGE:
            fields[SHADOW_LAST_CHANGE] = b""
        lines.append(b":".join(fields))
    return b"\n".join(lines)


def fingerprint(name: str, data: bytes) -> str | None:
    """A digest of what matters in a file, or None when the file is only a build timestamp."""
    if name.endswith(f".dist-info/{UV_CACHE}"):
        return None
    if name.endswith(".pyc") and len(data) >= PYC_HEADER:
        data = data[: PYC_MTIME[0]] + pyc_body(data)
    elif name in SHADOW:
        data = without_password_change_day(data)
    elif name.endswith(".dist-info/RECORD"):
        kept = [line for line in data.splitlines(keepends=True) if UV_CACHE.encode() not in line]
        data = b"".join(kept)
    return hashlib.sha256(data).hexdigest()


def entries(stream: IO[bytes]) -> Iterator[tuple[str, tuple]]:
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            digest = None
            if member.isreg():
                extracted = archive.extractfile(member)
                digest = fingerprint(member.name, extracted.read() if extracted else b"")
                if digest is None:
                    continue
            yield (
                member.name,
                (
                    member.type,
                    member.mode,
                    member.uid,
                    member.gid,
                    member.linkname,
                    digest,
                ),
            )


def differences(first: dict[str, tuple], second: dict[str, tuple]) -> list[str]:
    found = [f"only in first: {path}" for path in sorted(first.keys() - second.keys())]
    found += [f"only in second: {path}" for path in sorted(second.keys() - first.keys())]
    found += [
        f"differs: {path}"
        for path in sorted(first.keys() & second.keys())
        if first[path] != second[path]
    ]
    return found


def tree(image: str) -> dict[str, tuple]:
    container = subprocess.run(
        ["docker", "create", image], check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        assert export.stdout is not None
        result = dict(entries(export.stdout))
        if export.wait() != 0:
            raise RuntimeError(f"docker export failed for {image}")
        return result
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def report(found: Iterable[str], limit: int = 40) -> None:
    items = list(found)
    for item in items[:limit]:
        print(item, file=sys.stderr)
    if len(items) > limit:
        print(f"... and {len(items) - limit} more", file=sys.stderr)


def manifest_text(tree_entries: dict[str, tuple]) -> str:
    """A tree as JSON; the member type is the one bytes field of an entry.

    A .pyc digest depends on whether this interpreter could read the file (``pyc_body``), so the
    manifest names the interpreter's bytecode magic and only the same one may compare against it.
    """
    return json.dumps(
        {
            "bytecode": importlib.util.MAGIC_NUMBER.hex(),
            "entries": {
                path: [entry[0].decode(), *entry[1:]] for path, entry in tree_entries.items()
            },
        },
        sort_keys=True,
    )


class ManifestMismatch(Exception):
    """The manifest cannot be compared by this interpreter; the images were not judged."""


def load_manifest(text: str) -> dict[str, tuple]:
    """A manifest's entries in the form ``tree`` returns them."""
    manifest = json.loads(text)
    if manifest["bytecode"] != importlib.util.MAGIC_NUMBER.hex():
        raise ManifestMismatch(
            f"manifest was written by bytecode {manifest['bytecode']}, this Python is "
            f"{importlib.util.MAGIC_NUMBER.hex()}; compare with the Python minor of the image"
        )
    return {path: (entry[0].encode(), *entry[1:]) for path, entry in manifest["entries"].items()}


def compare(first: dict[str, tuple], second: dict[str, tuple]) -> int:
    found = differences(first, second)
    if found:
        print(f"images differ in {len(found)} paths:", file=sys.stderr)
        report(found)
        return 1
    print(f"images match: {len(first)} paths compared")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "--write-manifest":
        entries_of_image = tree(argv[2])
        with open(argv[3], "w", encoding="utf-8") as output:
            output.write(manifest_text(entries_of_image))
        print(f"manifest written: {len(entries_of_image)} paths")
        return 0
    if len(argv) == 4 and argv[1] == "--against-manifest":
        with open(argv[2], encoding="utf-8") as recorded:
            try:
                expected = load_manifest(recorded.read())
            except ManifestMismatch as error:
                print(f"not compared: {error}", file=sys.stderr)
                return 2
        return compare(expected, tree(argv[3]))
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    return compare(tree(argv[1]), tree(argv[2]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
