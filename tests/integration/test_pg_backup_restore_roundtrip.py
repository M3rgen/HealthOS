"""``scripts/backup.sh`` then ``scripts/restore.sh`` against a real PostgreSQL server.

The Compose CLI is the one thing replaced: a small ``docker`` shim runs the real ``pg_dump``,
``pg_restore``, ``psql``, ``dropdb`` and ``createdb`` client tools, connecting to the test
server as the scripts' containers would. Encryption, restore and every table's content are
real. Client tools: ``HEALTHOS_PG_DOCKER`` names a container on the same server (its own
binaries run through the real Docker CLI); otherwise ``HEALTHOS_PG_BIN``, otherwise
``pg_dump`` on ``PATH``, and they must be able to dump this server's major version.
``restore.sh`` needs a server built with libxml. ``HEALTHOS_REQUIRE_PG_CLIENT=1`` turns
any of these missing into a failure instead of a skip.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from identity_records import HEART, STEPS, heart, steps, store
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.archive.raw_history_verification import verify_guards

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
GPG = shutil.which("gpg")
PROCESS_GUARD = 180
DOCKER = shutil.which("docker") or "docker"
# gpg-agent's socket path must be short; pytest's macOS tmp_path is too long.
SHORT_TMP = "/tmp" if Path("/tmp").is_dir() else None
DOCKER_SHIM = r"""#!/usr/bin/env bash
set -euo pipefail
client() {
  tool="$1"
  shift
  if [ -n "${PG_DOCKER:-}" ]; then
    exec "$REAL_DOCKER" exec -i -e PGUSER -e PGPASSWORD "$PG_DOCKER" "$tool" "$@"
  fi
  exec "$PG_BIN/$tool" "$@"
}
if [ "$1" = compose ] && [ "$2" = --profile ]; then
  # compose --profile backup run --rm -T backup: the dump the backup service streams.
  client pg_dump --format=custom --compress=9 --dbname="$SOURCE_DB"
fi
# compose exec -T postgres TOOL ARGS...
[ "$1 $2 $3 $4" = "compose exec -T postgres" ]
shift 4
tool="$1"
shift
# The synthetic server has a different database name and data directory.
if [ "$tool" = df ]; then
  exec df -Pk "$HOME"
fi
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -U) shift 2 ;;  # compose user `health`; the test server's PGUSER is used instead
    -d)
      # The scripts name the live database `health`; here it is this test's database.
      if [ "$2" = health ]; then args+=(-d "$SOURCE_DB"); else args+=(-d "$2"); fi
      shift 2 ;;
    "SELECT pg_database_size('health')")
      args+=("SELECT pg_database_size(current_database())"); shift ;;
    *) args+=("$1"); shift ;;
  esac
done
client "$tool" "${args[@]}"
"""


def _skip_or_fail(message: str) -> None:
    if os.environ.get("HEALTHOS_REQUIRE_PG_CLIENT") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _major(version: str) -> int:
    match = re.search(r"(\d+)(?:\.\d+)?", version)
    assert match, f"unparsable version: {version!r}"
    return int(match.group(1))


class _Client:
    """PostgreSQL client tools reaching the test server, as binaries or inside a container."""

    def __init__(self, url, container: str, bin_dir: str) -> None:
        self.container = container
        self.bin_dir = bin_dir
        self.user = url.username or "postgres"
        self.database = url.database or ""
        # A container's tools use its local socket; host binaries use the server's TCP port.
        self.address = [] if container else ["-h", url.host or "", "-p", str(url.port or 5432)]
        self.env = {**os.environ, "PGUSER": self.user, "PGPASSWORD": url.password or ""}

    def run(self, tool: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        if self.container:
            argv = [DOCKER, "exec", "-e", "PGUSER", "-e", "PGPASSWORD", self.container, tool]
        else:
            argv = [str(Path(self.bin_dir) / tool)]
        return subprocess.run(
            [*argv, *args], capture_output=True, text=True, check=check, env=self.env
        )

    def psql(self, sql: str, check: bool = True) -> subprocess.CompletedProcess:
        return self.run("psql", *self.address, "-d", self.database, "-XAtc", sql, check=check)


@pytest.fixture
def client_tools(database_url) -> _Client:
    container = os.environ.get("HEALTHOS_PG_DOCKER", "")
    bin_dir = os.environ.get("HEALTHOS_PG_BIN", "")
    if not container and not bin_dir:
        found = shutil.which("pg_dump")
        bin_dir = str(Path(found).parent) if found else ""
    if not container and not (bin_dir and (Path(bin_dir) / "pg_dump").exists()):
        _skip_or_fail("set HEALTHOS_PG_DOCKER or HEALTHOS_PG_BIN to PostgreSQL client tools")
    client = _Client(make_url(database_url), container, bin_dir)
    server = client.psql("SHOW server_version").stdout
    dump = client.run("pg_dump", "--version").stdout.split()[-1]
    if _major(dump) < _major(server):
        _skip_or_fail(f"pg_dump {dump} cannot dump a server of version {server.strip()}")
    if client.psql("SELECT xml_is_well_formed('<a/>')", check=False).returncode != 0:
        _skip_or_fail("restore.sh needs a server built with libxml (query_to_xml)")
    return client


def _run(script: str, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        [BASH, str(ROOT / "scripts" / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=PROCESS_GUARD,
    )
    assert result.returncode == 0, f"{script} failed:\n{result.stderr}\n{result.stdout}"
    return result.stdout


async def _table_digests(sessions) -> dict[str, tuple[int, str]]:
    """Every table in public: row count and a digest of its rows."""
    async with sessions() as session:
        await session.execute(text("SET TIME ZONE 'UTC'"))  # one rendering of timestamptz
        names = list(
            await session.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            )
        )
        digests: dict[str, tuple[int, str]] = {}
        for name in names:
            row = (
                await session.execute(
                    text(
                        f"SELECT count(*), md5(coalesce(string_agg(t::text, $$,$$ "
                        f'ORDER BY t::text), $$$$)) FROM public."{name}" t'
                    )
                )
            ).one()
            digests[name] = (row[0], row[1])
    return digests


@pytest.mark.skipif(BASH is None or GPG is None, reason="bash and gpg are required")
async def test_encrypted_backup_restores_every_table_identically(
    pg_sessions, database_url, client_tools, tmp_path
):
    await store(pg_sessions, HEART, heart(60))
    await store(pg_sessions, HEART, heart(61))  # a second version: raw_health_data_versions
    await store(pg_sessions, STEPS, steps(120))

    url = make_url(database_url)
    target = f"healthos_restore_{uuid.uuid4().hex[:10]}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(DOCKER_SHIM, encoding="utf-8")
    docker.chmod(0o755)
    key_file = tmp_path / "backup.key"
    key_file.write_text("synthetic-passphrase-for-a-throwaway-archive\n", encoding="utf-8")
    key_file.chmod(0o600)
    gnupg = Path(tempfile.mkdtemp(prefix="hos-gpg-", dir=SHORT_TMP))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GNUPGHOME": str(gnupg),
        "BACKUP_HOST_DIR": str(tmp_path / "backups"),
        "BACKUP_STATUS_HOST_DIR": str(tmp_path / "status"),
        "BACKUP_KEY_FILE": str(key_file),
        "BACKUP_MIN_FREE_MB": "0",
        "PG_BIN": client_tools.bin_dir,
        "PG_DOCKER": client_tools.container,
        "REAL_DOCKER": DOCKER,
        "PGUSER": url.username or "postgres",
        "PGPASSWORD": url.password or "",
        "SOURCE_DB": url.database or "",
    }
    if client_tools.container:
        # Reach the real daemon: HOME is the sandbox here, the CLI's config is the user's.
        env["DOCKER_CONFIG"] = os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))
        env |= {k: os.environ[k] for k in ("DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
    else:
        env |= {"PGHOST": url.host or "", "PGPORT": str(url.port or 5432)}
    try:
        before = await _table_digests(pg_sessions)
        written = (await asyncio.to_thread(_run, "backup.sh", env)).strip().splitlines()[-1]
        backup = Path(written) if Path(written).is_absolute() else ROOT / written
        assert backup.suffix == ".gpg" and backup.read_bytes()[:5] != b"PGDMP"

        output = await asyncio.to_thread(_run, "restore.sh", env, str(backup), target)
        report = [line.split("\t") for line in output.splitlines() if line.count("\t") == 3]
        assert report[0] == ["table", "restored", "live", "comparison"]
        assert {row[3] for row in report[1:]} == {"equal"}
        assert {row[0] for row in report[1:]} == set(before)
        assert f"Alembic revision: {await _alembic_version(pg_sessions)}" in output

        restored_url = url.set(database=target)
        engine = create_async_engine(restored_url.render_as_string(hide_password=False))
        try:
            restored = async_sessionmaker(engine, expire_on_commit=False)
            assert await _table_digests(restored) == before
            async with restored() as session:
                assert await verify_guards(session) == []
            # The append-only guard came through the dump: a restored archive still refuses.
            with pytest.raises(DBAPIError):
                async with restored.begin() as session:
                    await session.execute(text("DELETE FROM raw_health_data"))
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(
            client_tools.run, "dropdb", *client_tools.address, "--if-exists", target
        )
        shutil.rmtree(gnupg, ignore_errors=True)


async def _alembic_version(sessions) -> str:
    async with sessions() as session:
        return await session.scalar(text("SELECT version_num FROM alembic_version"))
