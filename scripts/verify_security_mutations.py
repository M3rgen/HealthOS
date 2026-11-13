"""Check the four security regression tests in disposable source copies.

Run with the project's test interpreter: python scripts/verify_security_mutations.py.
No listener, database, credentials or network access is required. The socket-based
end-to-end tests remain in the suite; these checks also run in restricted sandboxes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_MODULE = "tests/test_security_invariants.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    before: str
    after: str
    tests: tuple[str, ...]


MUTATIONS = (
    Mutation(
        "session digest",
        "app/core/auth.py",
        "return hashlib.sha256(session_id.encode()).hexdigest()",
        "return session_id",
        (
            "test_a_session_id_is_kept_only_as_its_digest",
            "test_a_session_digest_is_not_itself_accepted",
        ),
    ),
    Mutation(
        "client TLS floor",
        "app/services/umay/codex_bridge_tls.py",
        "context.minimum_version = ssl.TLSVersion.TLSv1_3",
        "context.minimum_version = ssl.TLSVersion.TLSv1_2",
        ("test_client_tls_floor_without_a_listener",),
    ),
    Mutation(
        "client frame limit",
        "scripts/codex_bridge_front.py",
        "max_size=MAX_CLIENT_FRAME_BYTES,",
        "max_size=2**20,",
        ("test_front_passes_the_allowed_frame_size_to_the_listener",),
    ),
    Mutation(
        "HTTPS webhook host",
        "app/core/host_validation.py",
        'endpoint.scheme == "https" and endpoint.path == WEBHOOK_PATH',
        "endpoint.path == WEBHOOK_PATH",
        ("test_only_an_https_webhook_endpoint_opens_a_public_host",),
    ),
)


def copy_sources(destination: Path) -> None:
    """Copy only Python sources and pytest configuration, never local data or .env."""
    for directory in ("app", "scripts", "tests"):
        for source in (ROOT / directory).rglob("*.py"):
            if "__pycache__" in source.parts or source.is_symlink():
                continue
            target = destination / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for relative in ("pyproject.toml", "app/personas/umay/soul.md"):
        (destination / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination / relative)


def check(destination: Path, tests: tuple[str, ...], *, mutated: bool) -> None:
    """Require regression failures; skips and collection errors never kill a mutant."""
    report = destination / "result.xml"
    report.unlink(missing_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={report}",
            *(f"{TEST_MODULE}::{test}" for test in tests),
        ],
        cwd=destination,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
            },
            "PYTHONDONTWRITEBYTECODE": "1",
            "HEALTHOS_TEST_BACKEND": "sqlite",
            "APP_ENV": "test",
        },
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if not report.exists():
        raise RuntimeError(result.stdout + result.stderr)
    cases = ET.parse(report).findall(".//testcase")
    invalid = not cases or any(
        case.find(tag) is not None for case in cases for tag in ("error", "skipped")
    )
    failed = sum(case.find("failure") is not None for case in cases)
    if invalid or result.returncode != int(mutated) or bool(failed) != mutated:
        raise RuntimeError(f"Unexpected test outcome:\n{result.stdout}{result.stderr}")


def main() -> int:
    tests = tuple(test for mutation in MUTATIONS for test in mutation.tests)
    try:
        with tempfile.TemporaryDirectory(prefix="healthos-security-mutations-") as directory:
            destination = Path(directory)
            copy_sources(destination)
            check(destination, tests, mutated=False)
            print("Baseline passed (no skipped checks).", flush=True)
            for mutation in MUTATIONS:
                path = destination / mutation.path
                original = path.read_text()
                if mutation.before not in original:
                    raise RuntimeError(f"Mutation target missing: {mutation.name}")
                try:
                    # TLS has the same line on the server; mutate only the client's first match.
                    path.write_text(original.replace(mutation.before, mutation.after, 1))
                    check(destination, mutation.tests, mutated=True)
                finally:
                    path.write_text(original)
                print(f"Killed: {mutation.name}", flush=True)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
