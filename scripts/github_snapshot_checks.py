"""Publication checks for the immutable tree assembled by github_snapshot.sh."""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Generic private-network markers only. A maintainer's own markers (account names, home paths,
# host names) belong in the local, repeatable `healthos.githubForbidPattern` setting, never in
# this public file: listing them here, even split into pieces, would publish them.
DEFAULT_FORBID_PATTERNS = (
    rb"192\.168\.",
    rb"homelab\.local",
)

DEFAULT_DATA_JSON_LIMIT = 100_000
DATA_EXTENSIONS = frozenset(
    ".fit .tcx .gpx .xml .csv .tsv .zip .gz .tgz .tar .7z .sqlite .sqlite3 .db .dump .sql "
    ".gpg .age .parquet .ndjson .jsonl .xlsx .hdf5 .pkl".split()
)
# Exact path, SHA-256, and provenance. Changing a fixture requires a deliberate review here.
DATA_FILE_ALLOWLIST = {
    b"tests/fixtures/synthetic_activity.fit": (
        "b44fe76b904ebb0cdeec4125a29ddce6f0889ebd95fe71421d23ff55c0e95024",
        "Synthetic output of tests/fit_factory.py",
    ),
}
DATA_SIGNATURES = (
    (b"PK\x03\x04", "ZIP"),
    (b"\x1f\x8b", "gzip"),
    (b"SQLite format 3\0", "SQLite"),
    (b"PGDMP", "PostgreSQL dump"),
    (b"-----BEGIN PGP ", "OpenPGP armor"),
    (b"age-encryption.org/v1", "age"),
    (b"-----BEGIN AGE ENCRYPTED FILE-----", "age armor"),
    (b"PAR1", "Parquet"),
)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def reject(message: str, status: int = 1) -> None:
    print(f"github_snapshot: {message}", file=sys.stderr)
    raise SystemExit(status)


def commit_message(tree: str, supplied: str, source_short: str) -> str:
    if supplied:
        message = supplied
    else:
        try:
            changelog = git("show", f"{tree}:CHANGELOG.md").decode("utf-8")
        except (subprocess.CalledProcessError, UnicodeError):
            reject("the publication tree needs a readable CHANGELOG.md", 2)
        section = re.search(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
        if section is None or not re.search(r"^[-*] ", section[2], re.MULTILINE):
            reject("the first CHANGELOG.md version section needs a heading and bullet list", 2)
        message = f"{section[1].strip()}\n\n{section[2].strip()}"
    trace = re.compile(r"(?<!\w)dev ([0-9a-fA-F]{7,64})(?!\w)")
    references = trace.findall(message)
    if any(reference != source_short for reference in references):
        reject("commit message dev trace does not match the source commit")
    if re.search(r"eklenti/\S+|(?<!\w)[0-9a-f]{7,64}(?!\w)", trace.sub("", message), re.IGNORECASE):
        reject("commit message contains a private branch name or a hash-shaped word")
    if not references:
        message += ("" if message.endswith("\n") else "\n") + f"Source: dev {source_short}"
    return message


def check_patterns(tree: str, entries: list[tuple[bytes, bytes, bytes]]) -> None:
    config = subprocess.run(
        ["git", "config", "--local", "--null", "--get-all", "healthos.githubForbidPattern"],
        capture_output=True,
        check=False,
    )
    patterns = [value for value in config.stdout.split(b"\0") if value]
    if config.returncode not in (0, 1):
        reject("cannot read local healthos.githubForbidPattern values", 2)
    if not patterns:
        patterns = list(DEFAULT_FORBID_PATTERNS)
    found = False
    for number, pattern in enumerate(patterns, start=1):
        # Extended regular expressions, including binary blobs, without printing content.
        result = subprocess.run(
            [b"git", b"grep", b"-i", b"-l", b"-E", b"-e", pattern, tree.encode(), b"--"],
            capture_output=True,
            check=False,
        )
        paths = subprocess.run(
            [b"grep", b"-i", b"-E", b"-e", pattern],
            input=b"\n".join(path for _, _, path in entries) + b"\n",
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 1) or paths.returncode not in (0, 1):
            reject("cannot check forbidden patterns (invalid regular expression)", 2)
        if result.returncode == 0 or paths.returncode == 0:
            found = True
            print(
                f"github_snapshot: forbidden pattern {number} (content or path):", file=sys.stderr
            )
            for listing in (result.stdout, paths.stdout):
                if listing:
                    print(listing.decode("utf-8", errors="replace").rstrip(), file=sys.stderr)
    if found:
        reject("forbidden pattern in publication tree (content or path)")


def local_values(key: str) -> list[bytes]:
    result = subprocess.run(
        ["git", "config", "--local", "--null", "--get-all", key],
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        reject(f"cannot read local {key} values", 2)
    return result.stdout.split(b"\0")[:-1] if result.returncode == 0 else []


def data_file_settings() -> tuple[int, set[bytes]]:
    key = "healthos.githubDataJsonLimit"
    values = local_values(key)
    limit = DEFAULT_DATA_JSON_LIMIT
    if values:
        if len(values) != 1 or not re.fullmatch(rb"[0-9]+", values[0]):
            reject(f"{key} must be one positive byte count <= {DEFAULT_DATA_JSON_LIMIT}", 2)
        try:
            limit = int(values[0])
        except ValueError:
            reject(f"{key} is not a valid byte count", 2)
        if not 0 < limit <= DEFAULT_DATA_JSON_LIMIT:
            reject(f"{key} must be between 1 and {DEFAULT_DATA_JSON_LIMIT} bytes", 2)
    extensions = {value.encode() for value in DATA_EXTENSIONS}
    key = "healthos.githubDataForbidExtension"
    for value in local_values(key):
        if not re.fullmatch(rb"\.?[A-Za-z0-9][A-Za-z0-9._-]*", value):
            reject(f"{key} must name an extension without paths or wildcards", 2)
        extensions.add(b"." + value.lower().lstrip(b"."))
    return limit, extensions


def tree_blobs(entries: list[tuple[bytes, bytes, bytes]]) -> Iterator[tuple[bytes, bytes]]:
    # Read immutable objects: export-ignore, checkout filters and dirty files cannot hide data.
    if any(kind != b"blob" for kind, _, _ in entries):
        reject("cannot scan a publication tree containing submodules")
    objects = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=b"".join(oid + b"\n" for _, oid, _ in entries),
        capture_output=True,
        check=True,
    )
    stream = io.BytesIO(objects.stdout)
    for _, _, name in entries:
        size = int(stream.readline().split()[2])
        yield name, stream.read(size)
        stream.read(1)  # batch separator


def _is_utf8_text(prefix: bytes) -> bool:
    """True for a UTF-8 prefix without control bytes; a cut-off trailing character is tolerated."""
    for cut in range(4):
        try:
            text = prefix[: len(prefix) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return not any(ord(char) < 32 and char not in "\t\n\r" for char in text)
    return False


def data_signatures(data: bytes) -> list[str]:
    signatures = [label for prefix, label in DATA_SIGNATURES if data.startswith(prefix)]
    if data[8:12] == b".FIT":
        signatures.append("FIT")
    # An XML declaration or BOM may precede the Apple Health root/doctype.
    prefix = data[:4096]
    if re.match(
        rb"(?:\xef\xbb\xbf)?\s*(?:<\?xml\b[^>]*\?>\s*)?"
        rb"(?:<HealthData|<!DOCTYPE\s+HealthData)",
        prefix,
    ):
        signatures.append("Apple Health")
    # A UTF-8 text may begin with a multi-byte letter whose lead byte resembles a packet header.
    if len(data) >= 2 and data[0] & 0x80 and not _is_utf8_text(prefix):
        tag = data[0] & 0x3F if data[0] & 0x40 else (data[0] >> 2) & 0x0F
        # Session-key, encrypted-data and legacy marker packets (old and new headers).
        if tag in (1, 3, 9, 10, 18, 20):
            signatures.append("OpenPGP packet")
    return signatures


def check_data_files(entries: list[tuple[bytes, bytes, bytes]]) -> None:
    limit, extensions = data_file_settings()
    found = False
    for name, data in tree_blobs(entries):
        lower = name.lower()
        reasons = [
            f"extension {extension.decode()}"
            for extension in sorted(extensions)
            if lower.endswith(extension)
        ]
        reasons.extend(f"signature {label}" for label in data_signatures(data))
        if lower.endswith(b".json") and len(data) > limit:
            reasons.append(f"JSON size > {limit}")
        allowance = DATA_FILE_ALLOWLIST.get(name)
        if not reasons or (allowance and hashlib.sha256(data).hexdigest() == allowance[0]):
            continue
        found = True
        # repr escapes control characters in paths; neither content nor a content excerpt is logged.
        path = repr(name.decode("utf-8", errors="replace"))
        print(
            f"github_snapshot: data file {path}: {', '.join(reasons)}; {len(data)} bytes",
            file=sys.stderr,
        )
    if found:
        reject("data files in publication tree; no snapshot commit created")


def scan_tree(entries: list[tuple[bytes, bytes, bytes]]) -> None:
    scanner = shutil.which("gitleaks")
    if scanner is None:
        reject("gitleaks is required; install it before building a snapshot", 2)
    with tempfile.TemporaryDirectory(prefix="github-snapshot-tree-") as directory:
        root = Path(directory)
        # Store symlink blobs as text so their targets are checked too.
        for name, data in tree_blobs(entries):
            target = root / name.decode("utf-8", errors="surrogateescape")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        result = subprocess.run(
            [scanner, "dir", directory, "--redact", "--no-banner"],
            cwd=root,
            stdout=sys.stderr,
            check=False,
        )
        if result.returncode != 0:
            reject("gitleaks rejected the publication tree; no snapshot commit created")


def main() -> None:
    tree, supplied, source_short = sys.argv[1:]
    message = commit_message(tree, supplied, source_short)
    entries = []
    for entry in git("ls-tree", "-r", "-z", tree).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            _, kind, oid = metadata.split()
            entries.append((kind, oid, path))
    check_patterns(tree, entries)
    check_data_files(entries)
    scan_tree(entries)
    print(message, end="")


if __name__ == "__main__":
    main()
