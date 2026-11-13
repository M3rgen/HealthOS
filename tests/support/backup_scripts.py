"""Synthetic shell commands and isolated settings for backup/restore tests."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DUMP = b"PGDMP-synthetic-dump\n"
BASH = shutil.which("bash")
GPG = shutil.which("gpg")
GIB = 1024**3
# Bounds a stuck child; a healthy one on a loaded machine finishes far inside this.
PROCESS_GUARD = 120
needs_gpg = pytest.mark.skipif(GPG is None, reason="gpg is required")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"df -Pk /var/lib/postgresql/data"*)
    [ -z "${FAIL_SPACE_QUERY:-}" ] || exit 1
    printf '%s\\n' "${RESTORE_DF_OUTPUT-fake 999999999 1 104857600 1% /}" ;;
  *pg_database_size*)
    [ -z "${FAIL_SIZE_QUERY:-}" ] || exit 1
    printf '%s\\n' "${LIVE_DATABASE_BYTES-1073741824}" ;;
  *"--profile backup run --rm -T backup"*)
    [ -z "${FAKE_DOCKER_FAIL:-}" ] || exit 1
    printf '%s\\n' "${FAKE_DUMP_TEXT:-PGDMP-synthetic-dump}" ;;
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*) echo 20261017_0007 ;;
  *psql*) printf 'manual_measurements\\t1\\nraw_health_data\\t0\\n' ;;
esac
"""

# Real df unless FAKE_DF_AVAIL_KB is set; then a POSIX `df -Pk` line with that "Available".
FAKE_DF = """#!/usr/bin/env bash
if [ -n "${FAKE_DF_AVAIL_KB:-}" ]; then
  echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
  echo "fake 999999999 1 $FAKE_DF_AVAIL_KB 1% /"
  exit 0
fi
exec /bin/df "$@"
"""


@pytest.fixture
def sandbox(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    df = bin_dir / "df"
    df.write_text(FAKE_DF, encoding="utf-8")
    df.chmod(0o755)
    # gpg-agent's socket path must be short; pytest's macOS tmp_path is too long.
    gnupg = Path(tempfile.mkdtemp(prefix="hos-gpg-", dir="/tmp" if os.path.isdir("/tmp") else None))
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GNUPGHOME": str(gnupg),
        "BACKUP_HOST_DIR": str(tmp_path / "backups"),
        "BACKUP_STATUS_HOST_DIR": str(tmp_path / "metadata"),
        "BACKUP_KEY_FILE": str(tmp_path / "backup.key"),
        # Isolated from a real /etc/healthos/backup.env on the host running the tests.
        "BACKUP_ENV_FILE": str(tmp_path / "no-backup.env"),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_RESTORE_SINK": str(tmp_path / "restored.dump"),
        # 100 GiB free unless a test says otherwise, independent of the real disk.
        "FAKE_DF_AVAIL_KB": str(100 * 1024 * 1024),
    }
    yield env
    if shutil.which("gpgconf"):
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False)
    shutil.rmtree(gnupg, ignore_errors=True)


def run(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(ROOT / "scripts" / script), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_key(env: dict[str, str], mode: int = 0o600) -> Path:
    key = Path(env["BACKUP_KEY_FILE"])
    key.write_text("synthetic-test-passphrase-not-a-secret\n", encoding="utf-8")
    key.chmod(mode)
    return key


def status(env: dict[str, str]) -> dict:
    path = Path(env["BACKUP_HOST_DIR"]) / "last-backup-status.json"
    return json.loads(path.read_text(encoding="utf-8"))


def old_dumps(backups: Path, days: int = 20) -> tuple[list[Path], list[Path]]:
    """Old health-* dumps (prunable) and old non-dump files (never prunable)."""
    backups.mkdir(exist_ok=True)
    old = file_clock(backups) - days * 86_400
    dumps = [
        backups / "health-20250101T000000Z.dump.gpg",
        backups / "health-20250102T000000Z.dump.age",
        backups / "health-20250101.dump",
    ]
    others = [backups / "notes.txt", backups / "other-20250101.dump"]
    for path in (*dumps, *others):
        path.write_bytes(b"x")
        os.utime(path, (old, old))
    return dumps, others


def file_clock(directory: Path) -> float:
    """The filesystem's now, which `find -mtime` compares with, even under a shifted test clock."""
    stamp = directory / ".clock"
    stamp.touch()
    try:
        return stamp.stat().st_mtime
    finally:
        stamp.unlink()


def partials(backups: Path) -> list[Path]:
    return list(backups.glob("*.partial"))


def run_with_env_file(
    tmp_path: Path, env: dict[str, str], content: str
) -> subprocess.CompletedProcess[str]:
    # .env lives in the repository root; simulate it through a copy of the script tree.
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "backup.sh", tree / "scripts" / "backup.sh")
    shutil.copy(ROOT / "scripts" / "backup_env.sh", tree / "scripts")
    shutil.copy(ROOT / "scripts" / "write_backup_status.py", tree / "scripts")
    operations = Path("app/services/operations")
    (tree / operations).mkdir(parents=True)
    # Real packages prevent an editable install from supplying app from the worktree.
    for package in (Path("app"), Path("app/services"), operations):
        shutil.copy(ROOT / package / "__init__.py", tree / package)
    # The services package installs its stdlib-only alias finder on import.
    shutil.copy(ROOT / "app/services/_compat.py", tree / "app/services")
    for name in ("backup_status.py", "backup_files.py"):
        shutil.copy(ROOT / operations / name, tree / operations)
    (tree / ".env").write_text(content, encoding="utf-8")
    return subprocess.run(
        [BASH, str(tree / "scripts" / "backup.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def fake_age(sandbox):
    """Exercise pipeline completion/failure without a gpg-agent or encryption service."""
    binary = Path(sandbox["PATH"].split(os.pathsep)[0]) / "age"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ " $* " == *" --decrypt "* ]]; then\n'
        '  tail -n +2 "${@: -1}" | base64 -d; exit $?\n'
        "fi\n"
        '[ -z "${FAKE_ENCRYPT_EMPTY:-}" ] || { cat >/dev/null; exit 0; }\n'
        'printf "ciphertext-header\\n"\n'
        "base64\n"
        'if [ -n "${FAKE_ENCRYPT_GATE:-}" ]; then\n'
        '  touch "$FAKE_ENCRYPT_GATE.ready"\n'
        '  while [ ! -e "$FAKE_ENCRYPT_GATE.release" ]; do sleep 0.02; done\n'
        "fi\n"
        '[ -z "${FAKE_ENCRYPT_FAIL:-}" ] || exit 1\n'
    )
    binary.chmod(0o755)
    return {**sandbox, "BACKUP_AGE_RECIPIENT": "synthetic-public-recipient"}


@pytest.fixture
def fixed_age(fake_age):
    binary = Path(fake_age["PATH"].split(os.pathsep)[0]) / "date"
    binary.write_text(
        '#!/usr/bin/env bash\ncase "$*" in\n'
        "  *%Y%m%d*) echo 20261025T033000Z ;;\n"
        "  *) echo 2026-10-25T03:30:00Z ;;\nesac\n"
    )
    binary.chmod(0o755)
    return fake_age


def wait_for(path: Path):
    deadline = time.monotonic() + PROCESS_GUARD
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path.name}"
        time.sleep(0.02)  # poll interval of the bounded wait above


def _interactive_sigint() -> None:
    """A SIGINT that a background parent ignored would stay ignored in bash, whose traps then
    never run: the [INT] case hung for its whole budget whenever pytest was started with `&`.
    The script has to see the Ctrl-C a terminal delivers, whatever launched the test run."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@contextmanager
def backup_process(env):
    process = subprocess.Popen(
        [BASH, str(ROOT / "scripts" / "backup.sh")],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=_interactive_sigint,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=PROCESS_GUARD)


@pytest.fixture
def count_docker(sandbox, tmp_path):
    docker = Path(sandbox["PATH"].split(os.pathsep)[0]) / "docker"
    docker.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *"df -Pk /var/lib/postgresql/data"*)
    [ -z "${FAIL_SPACE_QUERY:-}" ] || exit 1
    printf '%s\\n' "${RESTORE_DF_OUTPUT-fake 999999999 1 104857600 1% /}" ;;
  *pg_database_size*)
    [ -z "${FAIL_SIZE_QUERY:-}" ] || exit 1
    printf '%s\\n' "${LIVE_DATABASE_BYTES-1073741824}" ;;
  *pg_restore*) cat > "$FAKE_RESTORE_SINK" ;;
  *alembic_version*)
    [ -z "${FAIL_REVISION_QUERY:-}" ] || exit 1
    echo 20261025_0020 ;;
  *psql*)
    case " $* " in
      *" -d health "*)
        [ -z "${FAIL_LIVE_QUERY:-}" ] || { echo "/private/query secret" >&2; exit 1; }
        printf '%s' "$LIVE_COUNTS" ;;
      *)
        [ -z "${FAIL_RESTORED_QUERY:-}" ] || exit 1
        printf '%s' "$RESTORED_COUNTS" ;;
    esac ;;
esac
""")
    docker.chmod(0o755)
    dump = tmp_path / "synthetic.dump"
    dump.write_bytes(DUMP)
    return {**sandbox, "RESTORED_COUNTS": "same\t2\n", "LIVE_COUNTS": "same\t2\n"}, dump


def comparisons(report):
    return {
        fields[0]: fields[1:]
        for fields in (line.split("\t") for line in report.splitlines())
        if len(fields) == 4 and fields[0] != "table"
    }


def fake_sudo(sandbox, tmp_path: Path) -> Path:
    """A `sudo` that only records how it was called; returns the file it records to."""
    calls = tmp_path / "sudo.log"
    sudo = Path(sandbox["PATH"].split(os.pathsep)[0]) / "sudo"
    sudo.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" > {calls}\nexit 0\n')
    sudo.chmod(0o755)
    return calls


def backup_env_file(sandbox, content: str) -> Path:
    path = Path(sandbox["BACKUP_ENV_FILE"])
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path
