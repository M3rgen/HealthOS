"""Build and verify an isolated, credential-free Compose stack."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

if __package__:
    from .compose_probes import (
        APP_FILES_SMOKE_SCRIPT,
        APP_ROLE_PROBE_SCRIPT,
        GARMIN_KEY_PROBE_SCRIPT,
        HEALTH_SMOKE_SCRIPT,
        UI_SMOKE_SCRIPT,
        verify_completed_migration,
    )
else:
    from compose_probes import (
        APP_FILES_SMOKE_SCRIPT,
        APP_ROLE_PROBE_SCRIPT,
        GARMIN_KEY_PROBE_SCRIPT,
        HEALTH_SMOKE_SCRIPT,
        UI_SMOKE_SCRIPT,
        verify_completed_migration,
    )

PROJECT_NAME = f"healthos-smoke-{os.getpid()}-{secrets.token_hex(4)}"
# Apple extension: its merge revision joins the Apple line to the main line.
EXPECTED_REVISION = "20261114_a005"
HOST_PORT = "18080"
APP_ROLE = "healthos_app"
ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / f".compose-smoke-backups-{os.getpid()}"
STATUS_DIR = ROOT / f".compose-smoke-status-{os.getpid()}"
# Matches the `.env*` ignore rules, so it never enters Git or the image build context.
SMOKE_ENV_FILE = ROOT / f".env.compose-smoke-{os.getpid()}"
SMOKE_KEY_FILE = ROOT / f".env.compose-smoke-backup-key-{os.getpid()}"
# Empty on purpose: Compose reads this instead of the checkout's own .env, so an operator's real
# settings (POSTGRES_APP_USER above all) cannot reach this throwaway stack or the drills.
SMOKE_NO_ENV_FILE = ROOT / f".env.compose-smoke-empty-{os.getpid()}"
# Hidden files in the ignored set, removed in the same finally block as everything else.
SMOKE_DRILL_DIR = ROOT / f".env.compose-smoke-drills-{os.getpid()}"
BASH = shutil.which("bash")


def smoke_env_values() -> dict[str, str]:
    """The ``KEY=value`` lines of the smoke env file, as a project .env would supply them."""
    if not SMOKE_ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for line in SMOKE_ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        name = name.strip()
        if separator and name and not name.startswith("#"):
            values[name] = value.strip()
    return values


def compose_image(service: str) -> str:
    """The pinned image of a service, read from the Compose file."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = text.split(f"\n  {service}:\n", 1)[1]
    match = re.search(r"^\s*image:\s*(\S+)", block, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"{service} has no image in docker-compose.yml")
    return match.group(1)


def check_checkout_is_shared(env: dict[str, str]) -> None:
    """Stop with a clear message when the Docker Engine cannot see this checkout.

    A Docker Engine inside a VM (Colima, Docker Desktop) sees only the host directories shared
    with it (Colima: the home directory by default). Elsewhere a bind mount silently creates an
    empty directory, and the smoke test only failed much later, on its backup metadata.
    """
    probe_file = STATUS_DIR / ".share-probe"
    probe_file.write_text("visible\n", encoding="utf-8")
    try:
        probe = subprocess.run(
            [
                DOCKER_COMMAND or "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                "-v",
                f"{STATUS_DIR}:/probe:ro",
                compose_image("postgres"),
                "-c",
                "test -f /probe/.share-probe",
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        probe_file.unlink(missing_ok=True)
    if probe.returncode != 0:
        raise RuntimeError(
            f"The Docker Engine cannot see this checkout ({ROOT}): a bind mount from it comes up "
            "empty in the container. Run make compose-smoke from a path the Engine shares: on "
            "Colima, the home directory by default, or add one with `colima start --mount "
            "<path>:w`; on Docker Desktop, Settings > Resources > File sharing. "
            f"(docker run exit {probe.returncode}: {probe.stderr.strip()[-400:]})"
        )


def backup_key_env() -> dict[str, str]:
    """A throwaway key for the encrypted backup/restore round trip (age, else gpg)."""
    SMOKE_KEY_FILE.unlink(missing_ok=True)
    if shutil.which("age") and shutil.which("age-keygen"):
        subprocess.run(["age-keygen", "-o", str(SMOKE_KEY_FILE)], check=True, capture_output=True)
    elif shutil.which("gpg"):
        SMOKE_KEY_FILE.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    else:
        raise RuntimeError("Encrypted backup verification needs age or gpg on the host")
    SMOKE_KEY_FILE.chmod(0o600)
    return {
        "BACKUP_KEY_FILE": str(SMOKE_KEY_FILE),
        # The host's real backup settings must not reach this throwaway stack.
        "BACKUP_ENV_FILE": str(ROOT / ".env.compose-smoke-no-backup-env"),
        "COMPOSE_PROJECT_NAME": PROJECT_NAME,
    }


def write_smoke_env_file() -> None:
    """Synthetic, non-placeholder values so the production guard (APP_ENV) passes."""
    overrides = {
        "GOOGLE_CLIENT_ID": "smoke-test-client-id",
        "GOOGLE_CLIENT_SECRET": secrets.token_urlsafe(24),
        "GOOGLE_TOKEN_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI": "https://webhook.example.com/webhooks/google-health",
        "GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION": f"Bearer {secrets.token_urlsafe(24)}",
        # In the file on purpose, like a real .env: the container must ignore it (it would
        # otherwise move the listener away from the published port and the healthcheck).
        "APP_PORT": HOST_PORT,
    }
    lines = [
        line
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.partition("=")[0].strip() not in overrides
    ]
    lines += [f"{name}={value}" for name, value in overrides.items()]
    SMOKE_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def free_port() -> str:
    """A random high port free on this host; parallel runs must not fight over one number.

    The Engine (possibly in a VM) can still hold it, so the range is wide on purpose.
    """
    for _ in range(50):
        port = str(secrets.randbelow(20000) + 20000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", int(port)))
            except OSError:
                continue
            return port
    raise RuntimeError("no free high port for the smoke test")


def find_docker() -> str | None:
    discovered = shutil.which("docker")
    if discovered:
        return discovered
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        docker_desktop_cli = (
            program_files / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        )
        if docker_desktop_cli.is_file():
            return str(docker_desktop_cli)
    return None


DOCKER_COMMAND = find_docker()


def compose(
    *arguments: str,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if DOCKER_COMMAND is None:
        raise RuntimeError("Docker CLI is unavailable")
    return subprocess.run(
        [DOCKER_COMMAND, "compose", "--project-name", PROJECT_NAME, *arguments],
        cwd=ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def docker(
    *arguments: str,
    env: dict[str, str],
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if DOCKER_COMMAND is None:
        raise RuntimeError("Docker CLI is unavailable")
    return subprocess.run(
        [DOCKER_COMMAND, *arguments],
        cwd=ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def published_ports(service: str, *, env: dict[str, str]) -> dict[str, object]:
    container_id = compose("ps", "--quiet", service, env=env, capture=True).stdout.strip()
    if not container_id:
        raise RuntimeError(f"No running container found for {service}")
    output = docker(
        "inspect",
        "--format",
        "{{json .NetworkSettings.Ports}}",
        container_id,
        env=env,
        capture=True,
    ).stdout
    return json.loads(output)


def verify_runtime(service: str, inspection: dict) -> None:
    """Check actual Engine settings, including long-syntax tmpfs mounts."""
    config = inspection["HostConfig"]
    if config.get("ReadonlyRootfs") is not True:
        raise RuntimeError(f"{service}: container root filesystem is writable")
    if "ALL" not in (config.get("CapDrop") or []):
        raise RuntimeError(f"{service}: container did not drop all Linux capabilities")
    options = [
        option.replace("=", ":").split(":", 1)
        for option in config.get("SecurityOpt") or []
        if option.replace("=", ":").split(":", 1)[0] == "no-new-privileges"
    ]
    if not options or any(len(option) > 1 and option[1] != "true" for option in options):
        raise RuntimeError(f"{service}: container lacks enabled no-new-privileges")
    temporary = ["/tmp", "/var/run/postgresql"] if service == "postgres" else ["/tmp"]
    mounts = config.get("Mounts") or []
    for target in temporary:
        short = (config.get("Tmpfs") or {}).get(target)
        long = [mount for mount in mounts if mount.get("Target") == target]
        if short is not None:
            if "ro" not in short.split(","):
                continue
        elif len(long) == 1 and long[0].get("Type") == "tmpfs" and not long[0].get("ReadOnly"):
            continue
        raise RuntimeError(f"{service}: missing writable tmpfs at {target}")
    # 0 and -1 are the Engine's "unlimited"; a memory or PID cap is a hardening control too.
    if not (config.get("Memory") or 0) > 0:
        raise RuntimeError(f"{service}: container has no memory limit")
    if not (config.get("PidsLimit") or 0) > 0:
        raise RuntimeError(f"{service}: container has no PID limit")
    if service == "postgres":
        if inspection["Config"].get("StopTimeout") != 60:
            raise RuntimeError("postgres: stop_grace_period did not reach the container")
        data = [
            mount
            for mount in inspection.get("Mounts", [])
            if mount.get("Destination") == "/var/lib/postgresql/data"
        ]
        if len(data) != 1 or not (
            data[0].get("Type") == "volume"
            and data[0].get("RW") is True
            and data[0].get("Name") == f"{PROJECT_NAME}_postgres_data"
        ):
            raise RuntimeError(
                "postgres: data must use the isolated project's writable named volume"
            )


def check_running_hardening(*, env: dict[str, str]) -> None:
    migration_id = compose(
        "ps", "--all", "--quiet", "migrate", env=env, capture=True
    ).stdout.strip()
    if not migration_id:
        raise RuntimeError("No completed migrate container found")
    migration = json.loads(docker("inspect", migration_id, env=env, capture=True).stdout)[0]
    verify_completed_migration(migration)
    verify_runtime("migrate", migration)
    for service in ("app", "postgres"):
        container_id = compose("ps", "--quiet", service, env=env, capture=True).stdout.strip()
        if not container_id:
            raise RuntimeError(f"No running container found for {service}")
        inspection = json.loads(docker("inspect", container_id, env=env, capture=True).stdout)[0]
        verify_runtime(service, inspection)
        # Exercise the permissions as the actual application/database user; a root
        # exec would hide a bad tmpfs mode. PID 1 must already have dropped root.
        if service == "postgres":
            compose(
                "exec",
                "-T",
                "--user",
                "postgres",
                service,
                "sh",
                "-ec",
                'test "$(awk \'/^Uid:/ {print $2}\' /proc/1/status)" = "$(id -u)"; '
                "for dir in /tmp /var/run/postgresql; do "
                'probe=$(mktemp "$dir/healthos-smoke.XXXXXX"); rm "$probe"; done',
                env=env,
            )
        else:
            compose(
                "exec",
                "-T",
                service,
                "python",
                "-c",
                APP_FILES_SMOKE_SCRIPT,
                env=env,
            )


def check_app_role(role: str, *, env: dict[str, str]) -> None:
    """The login the app actually uses: not a superuser, unable to disable the guards."""
    compose(
        "exec",
        "-T",
        "-e",
        f"EXPECTED_ROLE={role}",
        "app",
        "python",
        "-c",
        APP_ROLE_PROBE_SCRIPT,
        env=env,
    )


def check_host_port_reachable(*, port: str) -> None:
    """The published mapping itself, seen from the host: this is what APP_PORT must not break."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"app did not answer on host port {port}: {response.status}")


def key_file_owned_by(uid: int, directory: Path, *, env: dict[str, str]) -> None:
    """Create the synthetic key as the given owner, like `install -o` would on the host."""
    image = f"{PROJECT_NAME}-app"
    docker(
        "run",
        "--rm",
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/sh",
        "-v",
        f"{directory}:/k",
        image,
        "-ec",
        f"printf synthetic-garmin-key > /k/key; chown {uid}:{uid} /k/key; chmod 600 /k/key",
        env=env,
    )


def app_login_probe() -> tuple[str, ...]:
    return (
        "exec",
        "-T",
        "app",
        "python",
        "-c",
        "import os; print(os.environ['DATABASE_URL'].split('//')[1].split(':')[0])",
    )


def run_garmin_drill(*, env: dict[str, str]) -> None:
    """Garmin without editing a tracked file; the guard passes on the supported path only."""
    SMOKE_DRILL_DIR.mkdir(exist_ok=True)
    key_dir = SMOKE_DRILL_DIR / "garmin"
    key_dir.mkdir(exist_ok=True)
    garmin_env = {
        **env,
        "COMPOSE_FILE": os.pathsep.join(
            [str(ROOT / "docker-compose.yml"), str(ROOT / "docker-compose.garmin.yml")]
        ),
        "GARMIN_API_KEY_HOST_FILE": str(key_dir / "key"),
        # What a real .env selects; Compose hands the listed name to the app and migrate.
        "HEALTHOS_SOURCES": "garmin",
    }
    # Supported ownership: the key belongs to the container's UID, mode 600 on the host.
    key_file_owned_by(10001, key_dir, env=garmin_env)
    for service in ("migrate", "app"):
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            service,
            "python",
            "-c",
            GARMIN_KEY_PROBE_SCRIPT,
            env=garmin_env,
        )
    # A key the container cannot read must abort the migration, by name, not with a
    # traceback or by silently running without the source.
    # A key the container cannot read must abort the migration, by name. On a real Linux
    # file system (not a VM file share, which ignores the owner) a named volume is the
    # portable way to hold a root-owned, mode-600 file; the guard is the same.
    volume = f"{PROJECT_NAME}-unreadable-key"
    docker("volume", "create", volume, env=garmin_env, capture=True)
    try:
        docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{volume}:/k",
            f"{PROJECT_NAME}-app",
            "-ec",
            "printf synthetic-garmin-key > /k/key; chown 0:0 /k/key; chmod 600 /k/key",
            env=garmin_env,
            capture=True,
        )
        refused = compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            "GARMIN_API_KEY_FILE=/run/secrets/unreadable/key",
            "-v",
            f"{volume}:/run/secrets/unreadable:ro",
            "migrate",
            "alembic",
            "upgrade",
            "head",
            env=garmin_env,
            capture=True,
            check=False,
        )
    finally:
        docker("volume", "rm", "--force", volume, env=garmin_env, check=False, capture=True)
    if refused.returncode == 0 or "GARMIN_API_KEY_FILE" not in (refused.stderr or ""):
        raise RuntimeError(f"migrate did not refuse an unreadable key: {refused.stderr!r}")


def run_webhook_edge_drill(*, env: dict[str, str]) -> None:
    """Start the hardened caddy service; its published ports are removed for the test."""
    SMOKE_DRILL_DIR.mkdir(exist_ok=True)
    override = SMOKE_DRILL_DIR / "caddy-no-ports.yml"
    override.write_text("services:\n  caddy:\n    ports: !reset []\n", "utf-8")
    caddy_env = {
        **env,
        "COMPOSE_FILE": os.pathsep.join([str(ROOT / "docker-compose.yml"), str(override)]),
        "COMPOSE_PROFILES": "webhook",
    }
    compose("up", "--detach", "--no-deps", "caddy", env=caddy_env, capture=True)
    container = compose("ps", "--quiet", "caddy", env=caddy_env, capture=True).stdout.strip()
    if not container:
        raise RuntimeError("caddy did not start")
    inspection = json.loads(docker("inspect", container, env=caddy_env, capture=True).stdout)[0]
    verify_runtime("caddy", inspection)
    if inspection["HostConfig"].get("CapAdd") not in (
        ["NET_BIND_SERVICE"],
        ["CAP_NET_BIND_SERVICE"],
    ):
        raise RuntimeError(f"caddy: unexpected capabilities: {inspection['HostConfig']['CapAdd']}")
    # Binding 80/443 with only NET_BIND_SERVICE is what the capability set must allow.
    deadline = 40
    while deadline:
        logs = compose("logs", "caddy", env=caddy_env, capture=True, check=False).stdout or ""
        if "serving initial configuration" in logs or "server running" in logs:
            break
        status = docker(
            "inspect", "--format", "{{.State.Status}}", container, env=caddy_env, capture=True
        ).stdout.strip()
        if status != "running":
            raise RuntimeError(f"caddy exited: {logs[-2000:]}")
        deadline -= 1
        time.sleep(1)
    if not deadline:
        raise RuntimeError("caddy did not finish starting")
    compose("rm", "--force", "--stop", "caddy", env=caddy_env, capture=True)


def run_existing_install_upgrade_drill(*, env: dict[str, str]) -> None:
    """A separate stack: start without the role, write a record, opt in, the record survives."""
    project = f"{PROJECT_NAME}-existing"
    drill_env = {key: value for key, value in env.items() if not key.startswith("POSTGRES_APP_")}
    # A real older install: records and a superuser owner (dated opt-in); this stack states both.
    drill_env["POSTGRES_ALLOW_OWNER_RUNTIME"] = "1"
    drill_env["HEALTHOS_ALLOW_SUPERUSER_UNTIL"] = (date.today() + timedelta(days=1)).isoformat()
    drill_env["COMPOSE_PROJECT_NAME"] = project
    drill_env["APP_PORT"] = free_port()
    role_env = {
        **drill_env,
        "POSTGRES_APP_USER": APP_ROLE,
        "POSTGRES_APP_PASSWORD": secrets.token_hex(32),
    }

    def stack(*arguments: str, target: dict[str, str] = drill_env, capture: bool = True):
        return subprocess.run(
            [DOCKER_COMMAND or "docker", "compose", "--project-name", project, *arguments],
            cwd=ROOT,
            env=target,
            check=True,
            text=True,
            capture_output=capture,
        )

    def psql(sql: str, target: dict[str, str] = drill_env) -> str:
        output = stack(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "health",
            "-d",
            "health",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            sql,
            target=target,
        )
        return output.stdout.strip()

    try:
        # The file written for the main flow stays valid: the opt-in is only in interpolation.
        stack("up", "--detach", "--wait", "--wait-timeout", "180", "app")
        user = stack(*app_login_probe()).stdout.strip()
        if user != "health":
            raise RuntimeError(f"existing install changed connection user without opt-in: {user}")
        psql(
            "INSERT INTO manual_measurements (metric, value, measured_at, id) "
            "VALUES ('muscle', 41.5, now(), gen_random_uuid()) ON CONFLICT DO NOTHING"
        )
        before = psql("SELECT count(*) FROM manual_measurements")
        if psql("SELECT count(*) FROM pg_roles WHERE rolname = 'healthos_app'") != "0":
            raise RuntimeError("application role must not appear before the operator opts in")
        # Explicit opt-in: the two variables, then the normal `up`; no data is rewritten.
        stack("up", "--detach", "--wait", "--wait-timeout", "180", "app", target=role_env)
        compose_user = stack(*app_login_probe(), target=role_env).stdout.strip()
        if compose_user != APP_ROLE:
            raise RuntimeError(f"opt-in did not switch the application login: {compose_user}")
        if psql("SELECT count(*) FROM manual_measurements", role_env) != before:
            raise RuntimeError("application role opt-in changed the archive")
        stack(
            "exec",
            "-T",
            "-e",
            f"EXPECTED_ROLE={APP_ROLE}",
            "app",
            "python",
            "-c",
            APP_ROLE_PROBE_SCRIPT,
            target=role_env,
        )
        # Rollback is removing the two lines: the owner login still works, the role stays.
        stack("up", "--detach", "--wait", "--wait-timeout", "180", "app")
        user_back = stack(*app_login_probe()).stdout.strip()
        if user_back != "health":
            raise RuntimeError(f"opt-out did not restore the owner login: {user_back}")
    finally:
        subprocess.run(
            [
                DOCKER_COMMAND or "docker",
                "compose",
                "--project-name",
                project,
                "--profile",
                "backup",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=ROOT,
            env=drill_env,
            check=False,
            capture_output=True,
            text=True,
        )


def run_deployment_drills(*, env: dict[str, str]) -> None:
    """Optional paths that only real containers can prove."""
    run_garmin_drill(env=env)
    run_webhook_edge_drill(env=env)
    run_existing_install_upgrade_drill(env=env)


def check_persisted_measurement(*, env: dict[str, str]) -> None:
    rows = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "health",
        "-d",
        "health",
        "--tuples-only",
        "--no-align",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        "SELECT count(*) FROM manual_measurements WHERE metric='muscle' AND value=42.5",
        env=env,
        capture=True,
    ).stdout.strip()
    if rows != "1":
        raise RuntimeError(f"postgres: synthetic measurement did not persist: {rows!r}")


def verify_postgres_lifecycle(*, env: dict[str, str]) -> None:
    # First exercise a process restart, then the entrypoint on an existing data
    # directory in a fresh container. Neither operation removes the named volume.
    compose("restart", "postgres", env=env, capture=True)
    compose("up", "--detach", "--wait", "--wait-timeout", "180", "app", env=env, capture=True)
    check_persisted_measurement(env=env)
    check_running_hardening(env=env)
    compose(
        "up",
        "--detach",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "180",
        "postgres",
        "app",
        env=env,
        capture=True,
    )
    check_persisted_measurement(env=env)
    check_running_hardening(env=env)
    compose("exec", "-T", "app", "python", "-c", HEALTH_SMOKE_SCRIPT, env=env)


def verify_restore_counts(report: str, tables: set[str]) -> None:
    counts = {}
    try:
        rows = report.split("table\trestored\tlive\tcomparison\n", 1)[1].split(
            "\nCounts compare", 1
        )[0]
    except IndexError:
        raise RuntimeError("Missing restore comparison report") from None
    for line in rows.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            raise RuntimeError("Invalid restore comparison row")
        table, restored, live, comparison = fields
        if table in counts or not restored.isdecimal() or restored != live or comparison != "equal":
            raise RuntimeError("Controlled backup drill has unequal or invalid table counts")
        counts[table] = restored
    if set(counts) != tables:
        raise RuntimeError("Controlled backup drill did not compare every table")
    if counts.get("manual_measurements") != "1" or counts.get("raw_health_data") != "0":
        raise RuntimeError("Controlled backup drill is missing expected synthetic tables")


def expected_backup_reasons(expected: dict) -> list[str]:
    """What /health must list for the backup state ``expected`` describes."""
    if expected["status"] == "missing":
        return ["backup_missing"]
    return ["backup_failed"] if expected["last_attempt"]["result"] == "failure" else []


def check_backup_health(expected: dict, *, env: dict[str, str], attempts: int = 1) -> None:
    reasons = expected_backup_reasons(expected)
    script = "\n".join(
        [
            "import json, os, urllib.error, urllib.request",
            "url = 'http://127.0.0.1:8000/health/backup'",
            "try:",
            "    urllib.request.urlopen(url, timeout=5)",
            "    raise SystemExit('Backup health accepted an anonymous request')",
            "except urllib.error.HTTPError as error:",
            "    assert error.code == 401",
            "headers = {'Authorization': 'Bearer ' + os.environ['APP_UI_TOKEN']}",
            "request = urllib.request.Request(url, headers=headers)",
            "result = json.load(urllib.request.urlopen(request, timeout=5))",
            f"assert result == json.loads({json.dumps(expected)!r}), 'Backup metadata mismatch'",
            "request = urllib.request.Request('http://127.0.0.1:8000/health', headers=headers)",
            "health = json.load(urllib.request.urlopen(request, timeout=5))",
            "named = [item for item in health['reasons'] if item.startswith('backup_')]",
            f"assert named == {reasons!r}, ('Backup reason mismatch', named)",
        ]
    )
    for attempt in range(attempts):
        try:
            compose("exec", "-T", "app", "python", "-c", script, env=env)
            return
        except subprocess.CalledProcessError:
            if attempt == attempts - 1:
                raise
            time.sleep(1)


def write_smoked_image_manifest(env: dict[str, str]) -> None:
    """Record the tree of the image that passed, for release.yml to compare (set only in CI)."""
    target = os.environ.get("HEALTHOS_SMOKE_MANIFEST")
    if target:
        script = str(ROOT / ".github/scripts/compare_images.py")
        subprocess.run(
            [sys.executable, script, "--write-manifest", f"{PROJECT_NAME}-app", target],
            check=True,
            env=env,
            timeout=600,
        )


def main() -> int:
    if DOCKER_COMMAND is None:
        print(
            "Docker is unavailable; install/start Docker Engine or Docker Desktop.", file=sys.stderr
        )
        return 2
    if BASH is None:
        print("bash is required for scripts/backup.sh and scripts/restore.sh.", file=sys.stderr)
        return 2

    global HOST_PORT
    HOST_PORT = free_port()
    env = os.environ.copy()
    docker_bin = str(Path(DOCKER_COMMAND).parent)
    env.update(
        {
            # Pin the file for this process AND backup/restore subprocesses;
            # inherited development overrides must never select another database.
            "COMPOSE_FILE": str(ROOT / "docker-compose.yml"),
            "COMPOSE_PROFILES": "",
            "COMPOSE_ENV_FILES": str(SMOKE_NO_ENV_FILE),
            "APP_PORT": HOST_PORT,
            # Throwaway UI bearer token for the isolated stack (required in production).
            "APP_UI_TOKEN": secrets.token_urlsafe(32),
            "BACKUP_HOST_DIR": BACKUP_DIR.name,
            "BACKUP_STATUS_HOST_DIR": str(STATUS_DIR),
            # A throwaway database; do not demand the production 5 GiB headroom.
            "BACKUP_MIN_FREE_MB": "64",
            "PATH": docker_bin + os.pathsep + env.get("PATH", ""),
            # Generated like a real one: production holds the database password to the
            # same strength rule as every other secret.
            "POSTGRES_PASSWORD": secrets.token_hex(32),
            # A fresh install: the app connects as the non-superuser role, never the owner.
            "POSTGRES_APP_USER": APP_ROLE,
            "POSTGRES_APP_PASSWORD": secrets.token_hex(32),
            "RECONCILIATION_ENABLED": "false",
            "WATCHDOG_ENABLED": "false",
            "IDENTITY_RECONCILE_ENABLED": "false",
        }
    )
    cleanup_needed = False
    try:
        SMOKE_NO_ENV_FILE.write_text("", encoding="utf-8")
        write_smoke_env_file()
        # Compose reads the app's listed names from its own environment, like a project .env.
        for name, value in smoke_env_values().items():
            env.setdefault(name, value)
        compose("config", "--quiet", env=env)
        STATUS_DIR.mkdir(mode=0o755)  # host owns it; Docker must not create it as root
        STATUS_DIR.chmod(0o755)
        cleanup_needed = True
        check_checkout_is_shared(env)
        compose(
            "up",
            "--build",
            "--detach",
            "--wait",
            "--wait-timeout",
            "180",
            "app",
            env=env,
            capture=True,
        )

        verify_started_app(env)
        backup_env = verify_backup_restore(env)
        verify_live_restore(env, backup_env)
        verify_backup_failure(env, backup_env)

        verify_migration_lifecycle(env)

        postgres_ports = published_ports("postgres", env=env)
        if postgres_ports.get("5432/tcp") is not None:
            raise RuntimeError(
                f"PostgreSQL unexpectedly has a host binding: {postgres_ports['5432/tcp']}"
            )
        app_bindings = published_ports("app", env=env).get("8000/tcp")
        expected_binding = [{"HostIp": "127.0.0.1", "HostPort": HOST_PORT}]
        if app_bindings != expected_binding:
            raise RuntimeError(f"Unexpected app host binding: {app_bindings!r}")

        run_deployment_drills(env=env)
        write_smoked_image_manifest(env)

        print(
            "Compose smoke test passed: build, PostgreSQL, migration, health (token required), "
            "UI assets, dashboard, "
            "manual measurement, CSV, API, CLI, "
            "PostgreSQL restart/recreation persistence, backup/restore, "
            "hardened runtime and limits, bindings, APP_PORT in .env, the non-superuser role, "
            "Garmin override and key ownership, caddy, and the existing-install upgrade."
        )
        return 0
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Compose smoke test failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return 1
    finally:
        finish_smoke(env, cleanup_needed)


def verify_migration_lifecycle(env):
    # Every migration both ways, on an empty scratch database: a downgrade of the real
    # archive would (rightly) refuse once it holds evidence. Only the owner can migrate.
    lifecycle_db = "health_lifecycle"
    compose("exec", "-T", "postgres", "createdb", "-U", "health", lifecycle_db, env=env)
    lifecycle_env = (
        "-e",
        f"DATABASE_URL=postgresql+asyncpg://health:{env['POSTGRES_PASSWORD']}"
        f"@postgres:5432/{lifecycle_db}",
    )
    for command in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
        compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            *lifecycle_env,
            "migrate",
            "alembic",
            *command,
            env=env,
        )
    revision = compose(
        "run",
        "--rm",
        "--no-deps",
        "-T",
        *lifecycle_env,
        "migrate",
        "alembic",
        "current",
        env=env,
        capture=True,
    ).stdout
    if EXPECTED_REVISION not in revision:
        raise RuntimeError(f"Expected Alembic revision after lifecycle test; got {revision!r}")
    compose("exec", "-T", "postgres", "dropdb", "-U", "health", lifecycle_db, env=env)
    # The provisioner is rerun by every `up`; rerun it here too against the live archive.
    compose("run", "--rm", "--no-deps", "-T", "provision-app-role", env=env)
    check_app_role(APP_ROLE, env=env)


def verify_started_app(env):
    revision = compose("exec", "-T", "app", "alembic", "current", env=env, capture=True).stdout
    if EXPECTED_REVISION not in revision:
        raise RuntimeError(f"Expected Alembic revision {EXPECTED_REVISION}; got {revision!r}")

    compose(
        "exec",
        "-T",
        "app",
        "python",
        "-c",
        HEALTH_SMOKE_SCRIPT,
        env=env,
    )
    compose("exec", "-T", "app", "healthos", "status", env=env)
    compose(
        "exec",
        "-T",
        "app",
        "python",
        "-c",
        UI_SMOKE_SCRIPT,
        env=env,
    )
    app_uid = compose("exec", "-T", "app", "id", "-u", env=env, capture=True).stdout.strip()
    if app_uid != "10001":
        raise RuntimeError(f"Application container has unexpected UID {app_uid!r}")

    check_running_hardening(env=env)
    check_app_role(APP_ROLE, env=env)
    check_host_port_reachable(port=HOST_PORT)
    verify_postgres_lifecycle(env=env)
    check_backup_health(
        {"status": "missing", "last_attempt": None, "last_success_at": None}, env=env
    )


def frozen_tables(env):
    # Freeze the synthetic archive during dump and comparison. All schedulers
    # are disabled; stopping app also excludes startup/HTTP background writes.
    compose("stop", "app", env=env, capture=True)
    tables = set(
        compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "health",
            "-d",
            "health",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'",
            env=env,
            capture=True,
        ).stdout.splitlines()
    )

    return tables


def verify_backup_restore(env):
    tables = frozen_tables(env)
    backup_env = {
        **env,
        **backup_key_env(),
        "BACKUP_AGE_RECIPIENT": "",
        "BACKUP_AGE_IDENTITY": str(SMOKE_KEY_FILE),
        "BACKUP_RETENTION_DAYS": "never",
    }
    backup_path = Path(
        subprocess.run(
            [BASH, str(ROOT / "scripts" / "backup.sh")],
            cwd=ROOT,
            env=backup_env,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    )
    backup_files = sorted(BACKUP_DIR.glob("health-*.dump*"))
    if backup_files != [ROOT / backup_path] or backup_path.suffix not in {".age", ".gpg"}:
        raise RuntimeError(f"Expected one encrypted backup artifact; found {backup_files}")
    if (ROOT / backup_path).read_bytes()[:5] == b"PGDMP":
        raise RuntimeError("Backup artifact is an unencrypted pg_dump archive")
    backup_status = json.loads((BACKUP_DIR / "last-backup-status.json").read_text("utf-8"))
    if backup_status.get("result") != "success":
        raise RuntimeError(f"Backup status file does not report success: {backup_status}")
    restore_report = subprocess.run(
        [BASH, str(ROOT / "scripts" / "restore.sh"), str(backup_path), "health_restore"],
        cwd=ROOT,
        env=backup_env,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    verify_restore_counts(restore_report, tables)
    restored = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "health",
        "-d",
        "health_restore",
        "--tuples-only",
        "--no-align",
        "--command",
        ("SELECT count(*) FROM manual_measurements WHERE metric='muscle' AND value=42.5"),
        env=env,
        capture=True,
    ).stdout.strip()
    if restored != "1":
        raise RuntimeError(f"Backup restore verification returned {restored!r}")

    return backup_env


def restore_over_live_database(env, backup_env, backup_path):
    """docs/reference/data-safety.md, 'Destructive command': the steps a real restore runs."""
    method = backup_path.suffix
    if method == ".age":
        decrypt = ["age", "--decrypt", "--identity", backup_env["BACKUP_KEY_FILE"]]
    elif method == ".gpg":
        decrypt = [
            "gpg",
            "--batch",
            "--quiet",
            "--pinentry-mode",
            "loopback",
            "--passphrase-file",
            backup_env["BACKUP_KEY_FILE"],
            "--decrypt",
        ]
    else:
        raise RuntimeError(f"unexpected backup type: {backup_path}")
    with subprocess.Popen([*decrypt, str(backup_path)], stdout=subprocess.PIPE, env=env) as source:
        subprocess.run(
            [
                DOCKER_COMMAND or "docker",
                "compose",
                "--project-name",
                PROJECT_NAME,
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "health",
                "-d",
                "health",
                "--clean",
                "--if-exists",
                *("--no-owner", "--no-privileges", "--single-transaction"),
                "--exit-on-error",
            ],
            cwd=ROOT,
            env=env,
            stdin=source.stdout,
            check=True,
            capture_output=True,
        )
    compose("run", "--rm", "--no-deps", "-T", "provision-app-role", env=env, capture=True)


def verify_live_restore(env, backup_env):
    """Replace the live database from the backup: `healthos verify` must pass afterwards, and
    must still fail for a real tamper of a guard, not only a reformatted definition."""
    backups = sorted(BACKUP_DIR.glob("health-*.dump.*"))
    if len(backups) != 1:
        raise RuntimeError(f"Expected one backup to restore; found {backups}")
    restore_over_live_database(env, backup_env, backups[0])
    compose("up", "--detach", "--wait", "app", env=env, capture=True)
    verified = compose(
        "exec", "-T", "app", "healthos", "verify", env=env, capture=True, check=False
    )
    if verified.returncode != 0:
        raise RuntimeError(f"healthos verify failed after a live restore: {verified.stderr}")
    tamper = (
        "DROP TRIGGER raw_health_data_frozen_identity ON raw_health_data; "
        "CREATE TRIGGER raw_health_data_frozen_identity BEFORE UPDATE ON raw_health_data "
        "FOR EACH ROW WHEN (false) EXECUTE FUNCTION healthos_refuse_change(); "
        "ALTER TABLE raw_health_data ENABLE ALWAYS TRIGGER raw_health_data_frozen_identity"
    )
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "health",
        "-d",
        "health",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        tamper,
        env=env,
        capture=True,
    )
    tampered = compose(
        "exec", "-T", "app", "healthos", "verify", env=env, capture=True, check=False
    )
    if tampered.returncode == 0 or "redefined" not in (tampered.stdout or "") + (
        tampered.stderr or ""
    ):
        raise RuntimeError("healthos verify missed a tampered guard after a live restore")
    compose("stop", "app", env=env, capture=True)
    # Put the untampered guard back for the rest of the drill, the way an operator would.
    restore_over_live_database(env, backup_env, backups[0])


def verify_backup_failure(env, backup_env):
    compose("up", "--detach", "--wait", "app", env=env, capture=True)
    metadata = json.loads((STATUS_DIR / "status.json").read_text("utf-8"))
    expected = {key: metadata[key] for key in ("last_attempt", "last_success_at")}
    expected["status"] = "available"
    check_backup_health(expected, env=env)
    failed = subprocess.run(
        [BASH, str(ROOT / "scripts" / "backup.sh")],
        cwd=ROOT,
        env={**backup_env, "BACKUP_MIN_FREE_MB": "invalid"},
        text=True,
        capture_output=True,
        check=False,
    )
    if failed.returncode == 0:
        raise RuntimeError("Backup failure probe unexpectedly succeeded")
    latest = json.loads((STATUS_DIR / "status.json").read_text("utf-8"))
    if (
        latest["last_attempt"]["result"] != "failure"
        or latest["last_success_at"] != expected["last_success_at"]
    ):
        raise RuntimeError("Backup failure lost last success evidence")
    expected["last_attempt"] = latest["last_attempt"]
    # Atomic replacement must become visible without recreating the app; a bind mount
    # shows it at once, a virtualised file share (Colima, Docker Desktop) after its cache
    # timeout, so allow a few seconds, not a recreate.
    check_backup_health(expected, env=env, attempts=30)
    compose("up", "--detach", "--force-recreate", "--wait", "app", env=env, capture=True)
    check_backup_health(expected, env=env)


def remove_run_images(env):
    """Untag what this run built and nothing else: the prefix holds the run's random token."""
    prefix = f"{PROJECT_NAME}-"
    listing = docker(
        "image",
        "ls",
        "--filter",
        f"reference={prefix}*",
        "--format",
        "{{.Repository}}:{{.Tag}}",
        env=env,
        capture=True,
        check=False,
    )
    names = [name for name in (listing.stdout or "").splitlines() if name.startswith(prefix)]
    if names:
        docker("image", "rm", *names, env=env, capture=True, check=False)


def finish_smoke(env, cleanup_needed):
    try:
        cleanup_smoke(env, cleanup_needed)
    finally:
        if cleanup_needed:
            remove_run_images(env)


def cleanup_smoke(env, cleanup_needed):
    if cleanup_needed:
        compose(
            "down",
            "--volumes",
            "--remove-orphans",
            env=env,
            check=False,
        )
    SMOKE_ENV_FILE.unlink(missing_ok=True)
    SMOKE_NO_ENV_FILE.unlink(missing_ok=True)
    SMOKE_KEY_FILE.unlink(missing_ok=True)
    if SMOKE_DRILL_DIR.is_dir():
        if SMOKE_DRILL_DIR.resolve().parent != ROOT.resolve():
            raise RuntimeError("Refusing to remove a drill directory outside the workspace")
        # The Garmin key is root-owned after the drill; docker removes what the host cannot.
        docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{SMOKE_DRILL_DIR}:/d",
            f"{PROJECT_NAME}-app",
            "-c",
            "rm -rf /d/* /d/.[!.]*",
            env=env,
            check=False,
            capture=True,
        )
        shutil.rmtree(SMOKE_DRILL_DIR, ignore_errors=True)
    if STATUS_DIR.is_dir():
        if STATUS_DIR.resolve().parent != ROOT.resolve():
            raise RuntimeError("Refusing to remove status directory outside the workspace")
        for name in ("status.json", ".writer.lock"):
            (STATUS_DIR / name).unlink(missing_ok=True)
        STATUS_DIR.rmdir()
    if BACKUP_DIR.is_dir():
        resolved_root = ROOT.resolve()
        resolved_backup = BACKUP_DIR.resolve()
        if resolved_backup.parent != resolved_root:
            raise RuntimeError("Refusing to remove a backup directory outside the workspace")
        for backup_file in resolved_backup.glob("health-*.dump*"):
            backup_file.unlink()
        (resolved_backup / "last-backup-status.json").unlink(missing_ok=True)
        # backup.sh status temporaries: .last-backup-status.json.tmp.XXXXXXXX
        # (and the older fixed .last-backup-status.json.tmp).
        for status_file in resolved_backup.glob(".last-backup-status.json.tmp*"):
            status_file.unlink()
        resolved_backup.rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
