#!/usr/bin/env bash
# Runs deploy/systemd/healthos-backup.service for real: a transient unit built from the shipped
# file and its custom-backup-path drop-in (installation paths rewritten to throwaway paths)
# against a disposable PostgreSQL, with the ownership DEPLOYMENT.md leaves behind.
#
#   make test-backup-unit        (Linux with systemd and sudo, or macOS through Colima)
#
# Needs: systemd as PID 1, passwordless sudo, Docker with Compose, python3, age, age-keygen.
# Everything it creates is synthetic and removed afterwards: its own Compose project and
# volumes, the transient unit and one directory under /opt.
# Exit 0: passed. Exit 1: a check failed. Exit 2: this host cannot run the test.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(uname -s)" = Darwin ]; then
  command -v colima >/dev/null || {
    echo "verify_backup_unit: on macOS this needs a running Colima VM" >&2
    exit 2
  }
  # The repository must sit under a path Colima shares (the home directory by default).
  exec colima ssh -- bash "$ROOT/scripts/verify_backup_unit.sh" "$@"
fi

die() {
  echo "verify_backup_unit: $*" >&2
  exit 2
}

for tool in systemd-run systemctl sudo docker python3 age age-keygen; do
  command -v "$tool" >/dev/null || die "$tool is required"
done
[ "$(ps -o comm= 1)" = systemd ] || die "systemd is not PID 1 on this host"
sudo -n true 2>/dev/null || die "passwordless sudo is required"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is required"

id="$$-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
project="healthos-unit-$id"
unit="healthos-backup-test-$id"
# Not under a shared folder (the checks need real Linux ownership and modes) nor under /tmp or
# /var/tmp, which the unit's PrivateTmp hides.
base="$(sudo mktemp -d /opt/healthos-unit.XXXXXX)"
sudo chown "$(id -u):$(id -g)" "$base"
chmod 755 "$base"
app="$base/healthos"
etc="$base/etc-healthos"
SOURCE_UNIT="$ROOT/deploy/systemd/healthos-backup.service"
SOURCE_OVERRIDE="$ROOT/deploy/systemd/healthos-backup-paths.conf.example"
failed=0

compose() { (cd "$app" && docker compose "$@"); }

cleanup() {
  local rc=$?
  trap - EXIT
  sudo systemctl stop "$unit.service" >/dev/null 2>&1 || true
  sudo systemctl reset-failed "$unit.service" >/dev/null 2>&1 || true
  if [ -f "$app/docker-compose.yml" ]; then
    compose down -v --remove-orphans --timeout 1 >/dev/null 2>&1 || true
  fi
  sudo rm -rf "$base"
  exit "$rc"
}
trap cleanup EXIT

note() { printf '\n=== %s\n' "$*"; }
ok() { printf 'ok: %s\n' "$*"; }
bad() {
  printf 'FAILED: %s\n' "$*" >&2
  failed=1
}

note "stage: repository files, .env and backup-status as the deploy user (DEPLOYMENT steps 2, 4)"
mkdir -p "$app"
tar -C "$ROOT" -cf - --exclude='__pycache__' app scripts docker-compose.yml docker-compose.garmin.yml \
  | tar -C "$app" -xf -
secret() { python3 -c 'import secrets; print(secrets.token_hex(32))'; }
cat >"$app/.env" <<EOF
COMPOSE_PROJECT_NAME=$project
HEALTHOS_SOURCES=none
POSTGRES_PASSWORD=$(secret)
POSTGRES_APP_USER=healthos_app
POSTGRES_APP_PASSWORD=$(secret)
APP_UI_TOKEN=$(secret)
EOF
chmod 600 "$app/.env"
install -d -m 755 "$app/backup-status"
ls -ld "$app/.env" "$app/backup-status"

note "stage: backup key material under $etc (DEPLOYMENT step 8)"
age-keygen -o "$base/identity.age" 2>/dev/null
chmod 600 "$base/identity.age"
recipient="$(age-keygen -y "$base/identity.age")"
sudo install -d -m 700 "$etc"
printf 'BACKUP_AGE_RECIPIENT=%s\nBACKUP_MIN_FREE_MB=1\n' "$recipient" \
  | sudo tee "$etc/backup.env" >/dev/null
sudo chmod 600 "$etc/backup.env"

note "stage: disposable PostgreSQL with one synthetic row"
compose up -d --wait postgres
compose exec -T postgres psql -U health -d health -v ON_ERROR_STOP=1 -q \
  -c "CREATE TABLE unit_probe (id integer PRIMARY KEY)" -c "INSERT INTO unit_probe VALUES (1)" \
  -c "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)" \
  -c "INSERT INTO alembic_version VALUES ('unit-test')"

# The shipped unit, rewritten only where the paths differ. The test owns no copy of its options.
rewrite() {
  sed -e "s#/opt/healthos#$app#g" -e "s#/etc/healthos#$etc#g" \
    -e "s#/srv/healthos-backups#$base/custom-backups#g"
}
properties=()
exec_start=()
load_properties() {
  local line property name
  local kept=()
  while IFS= read -r line; do
    case "$line" in
      '' | '#'* | '['*) continue ;;
      ExecStart=*) read -r -a exec_start <<<"${line#ExecStart=}" ;;
      ExecStartPre= | ReadWritePaths=)
        # systemd empty assignments reset these lists when applying a drop-in.
        name="${line%=}"
        kept=()
        for property in "${properties[@]}"; do
          case "$property" in -p | "$name="*) continue ;; esac
          kept+=(-p "$property")
        done
        properties=("${kept[@]}") ;;
      *) properties+=(-p "$line") ;;
    esac
  done
}
load_properties < <(rewrite <"$SOURCE_UNIT")
[ "${#exec_start[@]}" -gt 0 ] || die "no ExecStart in $SOURCE_UNIT"

run_unit() {
  # run_unit SUFFIX COMMAND...: one transient run, under the unit's own options.
  local suffix="$1"
  shift
  sudo systemd-run --quiet --wait --collect --pipe --unit="$unit-$suffix" \
    "${properties[@]}" "$@"
}

journal_of() { sudo journalctl -q --no-pager -o cat -u "$unit-$1.service" | tail -n 30; }

check_status_published() {
  # read_backup_status is the function behind GET /health/backup.
  local report
  if report="$(sudo python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1])
from app.services.operations.backup_status import read_backup_status
from pathlib import Path
status = read_backup_status(Path(sys.argv[2]))
assert status["status"] == "available", status
assert status["last_attempt"]["result"] == "success", status
assert status["last_success_at"] == status["last_attempt"]["timestamp"], status
print(status)
' "$app" "$app/backup-status/status.json" 2>&1)"; then
    ok "/health/backup would report: $report"
  else
    bad "status.json does not report a success: $report"
  fi
}

note "run 1: first scheduled run, nothing but what the documentation prepared"
if run_unit first "${exec_start[@]}"; then
  ok "unit exited 0"
else
  bad "unit failed"
  journal_of first >&2
fi
dumps="$(sudo find "$app/backups" -maxdepth 1 -name 'health-*.dump.age' 2>/dev/null | wc -l | tr -d ' ')"
[ "$dumps" = 1 ] && ok "one encrypted dump written" || bad "expected one dump, found $dumps"
check_status_published
sudo stat -c '%U %a %n' "$app/backups" "$app/backup-status" "$app/backup-status/status.json" 2>&1 || true
mode="$(stat -c '%a' "$app/backup-status/status.json" 2>/dev/null || echo unreadable)"
[ "$mode" = 644 ] && ok "status.json is readable by the app container" \
  || bad "status.json mode is $mode, the app (UID 10001) needs 644"
[ -n "$(stat -c '%U' "$app/.env" | grep -v '^root$')" ] && ok ".env stayed with the deploy user" \
  || bad ".env was given to root"
[ "$(stat -c '%a' "$app/.env")" = 600 ] && ok ".env is still mode 600" || bad ".env mode changed"

note "the dump restores through the documented script"
newest="$(sudo find "$app/backups" -maxdepth 1 -name 'health-*.dump.age' 2>/dev/null | sort | tail -n 1)"
if [ -n "$newest" ] && restored="$(cd "$app" && BACKUP_AGE_IDENTITY="$base/identity.age" BACKUP_ENV_FILE="$etc/backup.env" \
  bash scripts/restore.sh "$newest" unit_restore 2>&1)"; then
  printf '%s\n' "$restored" | grep -E '^unit_probe' || true
  printf '%s\n' "$restored" | grep -Eq '^unit_probe[[:space:]]+1[[:space:]]+1[[:space:]]+equal' \
    && ok "restored row count matches" || bad "restored data does not match: $restored"
else
  bad "restore failed: ${restored:-no dump}"
fi

note "run 2: earlier backup made by the deploy user with make backup, then the timer"
sudo rm -rf "$app/backups" "$app/backup-status"
install -d -m 700 "$app/backups"
install -d -m 755 "$app/backup-status"
printf 'older synthetic dump\n' >"$app/backups/health-20200101T000000Z.dump.age"
chmod 600 "$app/backups/health-20200101T000000Z.dump.age"
printf '{"result":"success","timestamp":"2020-01-01T00:00:00Z"}\n' >"$app/backups/last-backup-status.json"
chmod 600 "$app/backups/last-backup-status.json"
: >"$app/backup-status/.writer.lock"
before="$(sha256sum "$app/backups/health-20200101T000000Z.dump.age" | cut -d' ' -f1)"
if run_unit second "${exec_start[@]}"; then
  ok "unit exited 0 with deploy-owned directories already in place"
else
  bad "unit failed on deploy-owned directories"
  journal_of second >&2
fi
after="$(sudo sha256sum "$app/backups/health-20200101T000000Z.dump.age" | cut -d' ' -f1)"
[ "$before" = "$after" ] && ok "the older dump is byte for byte unchanged" || bad "the older dump changed"
dumps="$(sudo find "$app/backups" -maxdepth 1 -name 'health-*.dump.age' 2>/dev/null | wc -l | tr -d ' ')"
[ "$dumps" = 2 ] && ok "the new dump sits beside it" || bad "expected two dumps, found $dumps"
check_status_published

note "probe: what the main process can and cannot reach under the same options"
probe='
set -u
grep -q "^CapEff:[[:space:]]*0000000000000000$" /proc/self/status && echo "ok: no effective capabilities" || { echo "FAILED: capabilities present"; exit 1; }
env_file="$PWD/.env"
if cat "$env_file" >/dev/null 2>&1; then echo "FAILED: the project .env is readable directly"; exit 1; fi
echo "ok: the deploy-owned .env is not readable directly"
test -r "$CREDENTIALS_DIRECTORY/healthos.env" && echo "ok: the private credential copy is readable" || { echo "FAILED: no credential copy"; exit 1; }
'
if run_unit probe /bin/sh -c "$probe"; then
  ok "sandbox holds"
else
  bad "sandbox probe failed"
fi

note "custom directory: apply the documented drop-in without weakening the sandbox"
custom="$base/custom-backups"
printf 'BACKUP_HOST_DIR=%s\n' "$custom" >>"$app/.env"
printf 'BACKUP_HOST_DIR=%s\n' "$custom" | sudo tee -a "$etc/backup.env" >/dev/null
load_properties < <(rewrite <"$SOURCE_OVERRIDE")
custom_probe="$probe"'
test -w "$1" || { echo "FAILED: the custom backup directory is not writable"; exit 1; }
if test -w "$2"; then echo "FAILED: the old backup directory is still writable"; exit 1; fi
echo "ok: only the custom backup directory is writable"
'
for attempt in custom-first custom-again; do
  if run_unit "$attempt" "${exec_start[@]}"; then
    ok "unit exited 0 with BACKUP_HOST_DIR outside the checkout ($attempt)"
  else
    bad "unit failed with the custom backup directory ($attempt)"
    journal_of "$attempt" >&2
  fi
  check_status_published
done
if run_unit custom-probe /bin/sh -c "$custom_probe" -- "$custom" "$app/backups"; then
  ok "custom-directory sandbox holds"
else
  bad "custom-directory sandbox probe failed"
fi
dumps="$(sudo find "$custom" -maxdepth 1 -name 'health-*.dump.age' 2>/dev/null | wc -l | tr -d ' ')"
[ "$dumps" = 2 ] && ok "both custom-directory dumps are retained" || bad "expected two custom dumps, found $dumps"
sudo python3 -c '
import json, sys
from pathlib import Path
directory = Path(sys.argv[1])
status = json.loads((directory / "last-backup-status.json").read_text())
assert status["result"] == "success", status
assert Path(status["file"]).parent == directory, status
' "$custom" && ok "detailed status points at the custom directory" || bad "custom status is invalid"
unchanged="$(sudo sha256sum "$app/backups/health-20200101T000000Z.dump.age" | cut -d' ' -f1)"
[ "$unchanged" = "$after" ] && ok "the original backup remains unchanged" || bad "the original backup changed"
dumps="$(sudo find "$app/backups" -maxdepth 1 -name 'health-*.dump.age' | wc -l | tr -d ' ')"
[ "$dumps" = 2 ] && ok "no further dump went to the old directory" || bad "the old backup directory was used"

echo
if [ "$failed" -ne 0 ]; then
  echo "verify_backup_unit: FAILED" >&2
  exit 1
fi
echo "verify_backup_unit: passed"
