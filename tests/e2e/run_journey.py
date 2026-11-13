"""One browser journey through the built frontend, the real FastAPI app and PostgreSQL.

    DATABASE_URL=postgresql+asyncpg://... make test-e2e

tests/browser/*.cjs answers every route from fixtures; this is the check that the two halves still
fit: login, panel, a measurement written through the real API, and the same page after a reload,
on a months-long synthetic archive (tests/e2e/seed_archive.py), under the one-screen rule.

DATABASE_URL must point at a disposable PostgreSQL on this machine; without one this fails, it does
not skip. Only a database this run creates is used, and only that one is dropped. The app runs in a
scratch directory with a minimal environment, so a developer's .env, tokens and sources are never
read. The frontend build is written to app/static, like `npx vite build`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[2]
JOURNEY = ROOT / "tests" / "e2e" / "journey.cjs"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# Synthetic, used only by this run; long enough to pass the production token rules.
UI_TOKEN = "e2e-journey-token-" + uuid.uuid4().hex
SERVER_WAIT_SECONDS = 60
JOURNEY_TIMEOUT_SECONDS = 600
# What the journey types in; the check after it reads this value back out of PostgreSQL.
JOURNEY_WEIGHT = "71.37"


def server_url() -> URL:
    raw = os.environ.get("DATABASE_URL", "")
    if not raw:
        raise SystemExit("make test-e2e needs DATABASE_URL=postgresql+asyncpg://... (disposable)")
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        raise SystemExit("make test-e2e runs on PostgreSQL only")
    if url.host not in LOOPBACK:
        raise SystemExit("make test-e2e refuses a database that is not on this machine")
    if not url.password:
        # Production settings refuse a URL without one; a local trust server accepts any password.
        raise SystemExit("make test-e2e needs a password in DATABASE_URL")
    return url


def render(url: URL, driver: str) -> str:
    return url.set(drivername=f"postgresql+{driver}").render_as_string(hide_password=False)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def clean_environment(database: str) -> dict[str, str]:
    """Only what the app needs: no inherited tokens, Google settings or test switches."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": database,
        "APP_ENV": "production",
        "APP_UI_TOKEN": UI_TOKEN,
        # A disposable server's login is usually its superuser, which production refuses
        # without this dated opt-in (app/core/role_policy.py).
        "HEALTHOS_ALLOW_SUPERUSER_UNTIL": (date.today() + timedelta(days=1)).isoformat(),
        # A disposable server's throwaway password (CI: ci-only-password) passes only the
        # earlier strength rule, which production accepts with this aid and a warning.
        "HEALTHOS_ALLOW_LEGACY_SECRETS": "true",
        "HEALTHOS_SOURCES": "none",
        "RECONCILIATION_ENABLED": "false",
        "WATCHDOG_ENABLED": "false",
        "IDENTITY_RECONCILE_ENABLED": "false",
    }


def wait_until_serving(url: str, process: subprocess.Popen, log: Path) -> None:
    request = urllib.request.Request(  # noqa: S310 - loopback URL built above
        f"{url}/health", headers={"Authorization": f"Bearer {UI_TOKEN}"}
    )
    deadline = time.monotonic() + SERVER_WAIT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the app exited during startup:\n{log.read_text()[-4000:]}")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except urllib.error.HTTPError as error:
            # Answered: the app is up, and the degraded states of a fresh archive are not 200.
            if error.code < 500 or error.code == 503:
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"the app did not answer within {SERVER_WAIT_SECONDS} s")


def stored_journey_weight(admin_url: str) -> list[Decimal]:
    engine = create_engine(admin_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT value FROM manual_measurements WHERE value = :v"),
                {"v": JOURNEY_WEIGHT},
            )
            return [row[0] for row in rows]
    finally:
        engine.dispose()


def run_journey(database_url: URL, name: str, scratch: Path) -> None:
    app_url = render(database_url.set(database=name), "asyncpg")
    environment = clean_environment(app_url)
    report = scratch / "archive.json"
    subprocess.run(
        [sys.executable, str(ROOT / "tests/e2e/seed_archive.py"), str(report)],
        check=True,
        cwd=scratch,
        env=environment,
        timeout=300,
    )
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log = scratch / "app.log"
    with log.open("wb") as stream:
        server = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
                "--no-proxy-headers",
            ],
            cwd=scratch,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_until_serving(base, server, log)
        print(f"e2e: app on {base}, database {name}")
        subprocess.run(
            ["node", str(JOURNEY)],  # noqa: S607 - Node from PATH, like the browser job
            check=True,
            cwd=ROOT / "frontend",
            env={
                **os.environ,
                "HEALTHOS_TEST_URL": base,
                # Python probes the bound address; Chromium resolves *.localhost itself.
                "HEALTHOS_E2E_CANONICAL_URL": f"http://healthos.localhost:{port}",
                "HEALTHOS_E2E_TOKEN": UI_TOKEN,
                "HEALTHOS_E2E_ARCHIVE": str(report),
                "HEALTHOS_E2E_WEIGHT": JOURNEY_WEIGHT,
            },
            timeout=JOURNEY_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError:
        print(f"e2e: the journey failed; app log:\n{log.read_text()[-4000:]}", file=sys.stderr)
        raise
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    stored = stored_journey_weight(render(database_url.set(database=name), "psycopg"))
    if stored != [Decimal(JOURNEY_WEIGHT)]:
        raise RuntimeError(f"PostgreSQL holds {stored} for the journey's measurement")
    print(f"e2e: PostgreSQL holds the measurement the browser wrote: {stored[0]}")


def main() -> int:
    url = server_url()
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("make test-e2e needs Node and npm")
    subprocess.run([npx, "vite", "build"], check=True, cwd=ROOT / "frontend", timeout=600)
    name = f"healthos_e2e_{uuid.uuid4().hex[:12]}"
    admin = create_engine(render(url, "psycopg"), isolation_level="AUTOCOMMIT")
    scratch = Path(tempfile.mkdtemp(prefix="healthos-e2e-"))
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        run_journey(url, name, scratch)
    finally:
        if created:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        shutil.rmtree(scratch, ignore_errors=True)
    print("e2e: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
