#!/usr/bin/env bash
# Encrypted, timestamped PostgreSQL backup for the Compose deployment.
#
# Runs on the Docker host (systemd timer: deploy/systemd/healthos-backup.timer).
# The Compose `backup` profile streams `pg_dump --format=custom` to stdout and
# this script encrypts the stream before anything touches the disk.
#
# Encryption key (never in .env, never next to the backups):
#   1. BACKUP_AGE_RECIPIENT=age1...  -> age, public key only on the host (preferred)
#   2. BACKUP_KEY_FILE (default /etc/healthos/backup.key, mode 600):
#        an age identity (AGE-SECRET-KEY-...) -> age
#        any other content                    -> gpg --symmetric passphrase
#   Keep a copy of the key (or the age identity) off this host, e.g. in a password
#   manager: without it every backup is unreadable.
#
# Backups are kept forever. Nothing is ever deleted unless BACKUP_RETENTION_DAYS
# is explicitly set (environment, else .env) to a positive whole number of days;
# unset, empty, 0, "never" or anything else means never delete (a value set in
# the environment, even empty, overrides .env). Old backups are never deleted to
# make room.
#
# Free-space guard: the script refuses to start (exit 1, no file written) when
# the backup directory has less free space than BACKUP_MIN_FREE_MB, or by
# default twice the newest dump (at least 1 GiB), or 5 GiB before the first
# dump. BACKUP_MIN_FREE_MB=0 disables the guard.
#
# Every run writes <BACKUP_HOST_DIR>/last-backup-status.json (result, time,
# file, size, free space, message), on success and on failure.
#
# Other settings: BACKUP_HOST_DIR (default ./backups). Settings come from the
# environment, then BACKUP_ENV_FILE (default /etc/healthos/backup.env, the file the
# systemd unit loads), then the project's .env; the key and recipient only from the
# first two. stdout carries only the path of the new backup; everything else goes to
# stderr.
#
# Host tools: bash (3.2 or newer, so macOS works too), docker compose, age or
# gpg, and Python 3.11+ (stdlib only) for the metadata. BACKUP_PYTHON names the
# interpreter; otherwise the first Python 3.11+ among python3, python3.13,
# python3.12, python3.11 and the checkout's .venv is used, else python3.
set -euo pipefail
umask 077
# Host-side helpers must also work with a read-only source tree.
export PYTHONDONTWRITEBYTECODE=1

# shellcheck source=scripts/backup_env.sh
. "$(dirname "${BASH_SOURCE[0]}")/backup_env.sh"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GIB=$((1024 * 1024 * 1024))
STATUS_NAME=last-backup-status.json

pick_python() {
  # Older system interpreters (macOS 3.9, Ubuntu 22.04 3.10) cannot publish the
  # metadata; prefer any 3.11+ that is present. Same scripts, same arguments.
  local candidate
  if [ -n "${BACKUP_PYTHON:-}" ]; then
    printf '%s' "$BACKUP_PYTHON"
    return 0
  fi
  for candidate in python3 python3.13 python3.12 python3.11 "$ROOT/.venv/bin/python"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  printf 'python3'
}
PYTHON="$(pick_python)"

# The file the systemd unit loads with EnvironmentFile= (DEPLOYMENT step 8), read here too so
# `make backup` behaves like the timer.
backup_env_file="${BACKUP_ENV_FILE:-/etc/healthos/backup.env}"

# The systemd unit runs without capabilities and cannot read the deploy user's .env (mode 600):
# LoadCredential= hands this run a private copy, and Compose gets the same copy. Elsewhere the
# project's own .env is used as always.
project_env=.env
compose_env=()
if [ -n "${CREDENTIALS_DIRECTORY:-}" ] && [ -r "$CREDENTIALS_DIRECTORY/healthos.env" ]; then
  project_env="$CREDENTIALS_DIRECTORY/healthos.env"
  compose_env=(--env-file "$project_env")
fi

env_file_value() { file_value "$project_env" "$1"; }
backup_env_value() { file_value "$backup_env_file" "$1"; }
in_backup_env() { [ -r "$backup_env_file" ] && grep -Eq "^[[:space:]]*$1[[:space:]]*=" "$backup_env_file"; }

setting() {
  # Environment, then backup.env, then .env; keys and recipient never come from .env.
  local name="$1" value
  value="${!name:-}"
  [ -n "$value" ] || value="$(backup_env_value "$name")"
  [ -n "$value" ] || value="$(env_file_value "$name")"
  printf '%s' "$value"
}

backup_dir="$(setting BACKUP_HOST_DIR)"
backup_dir="${backup_dir:-./backups}"
status_dir="$(setting BACKUP_STATUS_HOST_DIR)"
status_dir="${status_dir:-./backup-status}"
key_file="${BACKUP_KEY_FILE:-$(backup_env_value BACKUP_KEY_FILE)}"
key_file="${key_file:-/etc/healthos/backup.key}"
# Up to 0.2.2 the installer kept user keys under the misspelled name (~/.config/healtos,
# ~/Library/Application Support/HealtOS). It now copies them to the healthos directory; until
# that has happened, a missing key is looked up at the old path.
if [ ! -e "$key_file" ]; then
  legacy_key="$(printf '%s' "$key_file" | sed -e 's|/healthos/|/healtos/|' -e 's|/HealthOS/|/HealtOS/|')"
  if [ "$legacy_key" != "$key_file" ] && [ -f "$legacy_key" ]; then
    echo "backup: $key_file not found; using the old path $legacy_key (re-run the installer to copy it)" >&2
    key_file="$legacy_key"
  fi
fi
recipient="${BACKUP_AGE_RECIPIENT:-$(backup_env_value BACKUP_AGE_RECIPIENT)}"
compose_file="$(backup_env_value COMPOSE_FILE)"
if [ -z "${COMPOSE_FILE:-}" ] && [ -n "$compose_file" ]; then export COMPOSE_FILE="$compose_file"; fi

# The unit creates ./backups mode 700 owned by root and backup.env is mode 600, so a Docker-group
# account cannot back up: rerun as root (the unit runs as root too) rather than fail half-way.
if [ "$(id -u)" != 0 ]; then
  blocked=""
  backup_env_dir="$(dirname "$backup_env_file")"
  # DEPLOYMENT step 8 makes /etc/healthos mode 700: the file inside cannot even be seen.
  if [ -d "$backup_env_dir" ] && [ ! -x "$backup_env_dir" ]; then
    blocked="$backup_env_dir is accessible only by root"
  elif [ -e "$backup_env_file" ] && [ ! -r "$backup_env_file" ]; then
    blocked="$backup_env_file is readable only by root"
  elif [ -d "$backup_dir" ] && [ ! -w "$backup_dir" ]; then
    blocked="$backup_dir is writable only by root"
  elif [ -d "$status_dir" ] && [ ! -w "$status_dir" ]; then
    blocked="$status_dir is writable only by root"
  fi
  if [ -n "$blocked" ]; then
    echo "backup: $blocked; rerunning with sudo (the timer runs as root too)." >&2
    command -v sudo >/dev/null || {
      echo "backup: sudo is not installed; rerun this script as root." >&2
      exit 1
    }
    # sudo resets the environment: hand over the settings given to this run.
    passed=()
    for name in BACKUP_ENV_FILE BACKUP_AGE_RECIPIENT BACKUP_KEY_FILE BACKUP_HOST_DIR \
      BACKUP_STATUS_HOST_DIR BACKUP_RETENTION_DAYS BACKUP_MIN_FREE_MB COMPOSE_FILE; do
      [ -z "${!name+set}" ] || passed+=("$name=${!name}")
    done
    exec sudo ${passed[@]+"${passed[@]}"} bash "$ROOT/scripts/backup.sh"
  fi
fi

output=""
partial=""
status_partial=""
size_bytes=""
required_bytes=""
retention_label="never"
pruned_count=0
failure_reason=""
status_written=0

json_string() {
  printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037')"
}

json_number() {
  if [ -n "$1" ]; then printf '%s' "$1"; else printf 'null'; fi
}

free_bytes_of() {
  # POSIX df: 1024-byte blocks, "Available" is column 4 of the last line.
  local kib
  kib="$(df -Pk "$1" 2>/dev/null | awk 'END { print $4 }')" || return 1
  case "$kib" in '' | *[!0-9]*) return 1 ;; esac
  printf '%s' "$((kib * 1024))"
}

human() {
  awk -v b="$1" 'BEGIN { printf "%.2f GiB", b / 1073741824 }'
}

publish_status_shell() {
  # Emergency metadata publication without any host Python dependency.
  (
    local result="$1" timestamp="$2" reason="$3" previous success=null tmp
    local time_pattern='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
    case "$result" in success | failure) ;; *) exit 1 ;; esac
    [[ "$timestamp" =~ $time_pattern && "$reason" =~ ^[a-z_]{1,64}$ ]] || exit 1
    mkdir -p "$status_dir" && chmod 755 "$status_dir" || exit 1
    # Keep the read and replace under the Python writer's lock when available.
    # The subshell closes this descriptor on every exit path.
    if command -v flock >/dev/null; then
      exec 9>"$status_dir/.writer.lock" || exit 1
      flock -x 9 || exit 1
    fi
    previous="$(sed -n 's/.*"last_success_at":[[:space:]]*"\([^"]*\)".*/\1/p' \
      "$status_dir/status.json")" || previous=""
    if [[ "$previous" =~ $time_pattern ]]; then success="\"$previous\""; fi
    if [ "$result" = success ]; then success="\"$timestamp\""; fi
    tmp="$(mktemp "$status_dir/.status-XXXXXXXX")" || exit 1
    if ! {
      printf '{"schema_version": 1, "last_attempt": {"result": "%s", "timestamp": "%s"}, "last_success_at": %s, "failure_reason": "%s"}\n' \
        "$result" "$timestamp" "$success" "$reason" >"$tmp" \
        && chmod 644 "$tmp" && mv -f "$tmp" "$status_dir/status.json"
    }; then
      rm -f "$tmp"
      exit 1
    fi
  ) 2>/dev/null || echo "backup: could not publish status metadata" >&2
  return 0
}

write_status() {
  # write_status RESULT MESSAGE; atomic replace, never fatal.
  local result="$1" message="$2" free tmp file_json timestamp
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # Separate directory: only sanitized metadata is mounted into the app. Publish
  # before replacing the private legacy file, so its old success can be migrated.
  if ! "$PYTHON" "$ROOT/scripts/write_backup_status.py" "$status_dir" \
    "$backup_dir/$STATUS_NAME" "$result" "$timestamp" 2>/dev/null; then
    publish_status_shell "$result" "$timestamp" host_helper_failed
  fi
  mkdir -p "$backup_dir" 2>/dev/null || return 0
  free="$(free_bytes_of "$backup_dir" || true)"
  if [ -n "$output" ] && [ "$result" = success ]; then
    file_json="$(json_string "$output")"
  else
    file_json=null
  fi
  tmp="$(mktemp "$backup_dir/.$STATUS_NAME.tmp.XXXXXXXX" 2>/dev/null)" || {
    echo "backup: could not allocate legacy status metadata" >&2
    return 0
  }
  status_partial="$tmp"
  if ! {
    printf '{\n'
    printf '  "timestamp": %s,\n' "$(json_string "$timestamp")"
    printf '  "result": %s,\n' "$(json_string "$result")"
    printf '  "message": %s,\n' "$(json_string "$message")"
    printf '  "file": %s,\n' "$file_json"
    printf '  "size_bytes": %s,\n' "$(json_number "$size_bytes")"
    printf '  "free_bytes": %s,\n' "$(json_number "$free")"
    printf '  "required_free_bytes": %s,\n' "$(json_number "$required_bytes")"
    printf '  "retention_days": %s,\n' "$(json_string "$retention_label")"
    printf '  "pruned_files": %s\n' "$pruned_count"
    printf '}\n'
  } >"$tmp" 2>/dev/null || ! mv -f "$tmp" "$backup_dir/$STATUS_NAME" 2>/dev/null; then
    rm -f "$tmp" 2>/dev/null
    status_partial=""
    echo "backup: could not write $backup_dir/$STATUS_NAME" >&2
    return 0
  fi
  status_partial=""
  status_written=1
}

on_exit() {
  local rc=$?
  [ -z "$partial" ] || rm -f "$partial"
  [ -z "$status_partial" ] || rm -f "$status_partial"
  if [ "$rc" -ne 0 ] && [ "$status_written" = 0 ]; then
    write_status failure "${failure_reason:-backup.sh exited with status $rc}"
  fi
}
trap on_exit EXIT
# Make a systemd stop/timeout or Ctrl-C run the EXIT handler too.
trap 'exit 143' TERM HUP
trap 'exit 130' INT

fail() {
  failure_reason="$*"
  echo "backup: $*" >&2
  exit 1
}

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  python_version="$("$PYTHON" -V 2>&1)" || python_version=none
  fail "needs python3 >= 3.11 on the host (found: $python_version); see docs/DEPLOYMENT.md"
fi

# Retention is opt-in: old dumps are deleted only when BACKUP_RETENTION_DAYS is a
# positive whole number of days. Set in the environment (even to an empty value), the
# environment wins and .env is not read; otherwise .env decides. Anything else (unset,
# empty, 0, never, negative, not a number) deletes nothing; an unrecognised value only
# warns, so a typo can neither delete backups nor stop them. 36500 (about 100 years)
# works and is effectively never.
if [ -n "${BACKUP_RETENTION_DAYS+set}" ]; then
  retention_days="$BACKUP_RETENTION_DAYS"
elif in_backup_env BACKUP_RETENTION_DAYS; then
  retention_days="$(backup_env_value BACKUP_RETENTION_DAYS)"
else
  retention_days="$(env_file_value BACKUP_RETENTION_DAYS)"
fi
case "$(printf '%s' "$retention_days" | tr '[:upper:]' '[:lower:]')" in
  '' | never | *[!0-9]*)
    case "$retention_days" in
      '' | [Nn][Ee][Vv][Ee][Rr]) ;;
      *) echo "backup: BACKUP_RETENTION_DAYS='$retention_days' is not a positive whole number of days; keeping every backup" >&2 ;;
    esac
    retention_days="" ;;
  *)
    retention_days="$(printf '%s' "$retention_days" | sed 's/^0*//')"
    # 0 keeps everything; more than 9 digits is beyond any lifetime (and bash arithmetic).
    [ "${#retention_days}" -le 9 ] || retention_days="" ;;
esac
[ -z "$retention_days" ] || retention_label="$retention_days"

min_free_mb="$(setting BACKUP_MIN_FREE_MB)"
case "$min_free_mb" in
  *[!0-9]*) fail "BACKUP_MIN_FREE_MB must be a whole number of MiB (0 disables the free-space guard)" ;;
esac

check_key_file() {
  [ -f "$key_file" ] || return 1
  [ -r "$key_file" ] || fail "$key_file is not readable by $(id -un)"
  # Group/other permission bits must all be unset (chmod 600 or 400). ls -l is the portable
  # (GNU and BSD) way to read the mode string.
  # shellcheck disable=SC2012
  [ "$(ls -ln "$key_file" | cut -c5-10)" = "------" ] \
    || fail "$key_file must not be readable by group or others; run: chmod 600 $key_file"
  [ -s "$key_file" ] || fail "$key_file is empty"
}

if [ -n "$recipient" ]; then
  command -v age >/dev/null || fail "BACKUP_AGE_RECIPIENT is set but age is not installed"
  method=age
  encrypt=(age --encrypt --recipient "$recipient")
elif check_key_file; then
  if grep -q '^AGE-SECRET-KEY-' "$key_file"; then
    command -v age >/dev/null || fail "$key_file is an age identity but age is not installed"
    method=age
    encrypt=(age --encrypt --identity "$key_file")
  else
    command -v gpg >/dev/null || fail "gpg is required for a passphrase key file"
    method=gpg
    encrypt=(gpg --batch --quiet --yes --pinentry-mode loopback --symmetric
      --cipher-algo AES256 --passphrase-file "$key_file" --output -)
  fi
else
  fail "no backup key: set BACKUP_AGE_RECIPIENT (e.g. in /etc/healthos/backup.env) or create $key_file (mode 600); keys do not belong in .env"
fi

mkdir -p "$backup_dir" || fail "cannot create $backup_dir"
chmod 700 "$backup_dir" 2>/dev/null || true

# Free-space guard: refuse before anything is written; never delete to make room.
newest=""
for candidate in "$backup_dir"/health-*.dump "$backup_dir"/health-*.dump.age "$backup_dir"/health-*.dump.gpg; do
  [ -f "$candidate" ] || continue
  if [ -z "$newest" ] || [ "$candidate" -nt "$newest" ]; then newest="$candidate"; fi
done
if [ -n "$min_free_mb" ]; then
  required_bytes=$((10#$min_free_mb * 1024 * 1024))
  basis="BACKUP_MIN_FREE_MB=$min_free_mb"
elif [ -n "$newest" ]; then
  required_bytes=$((2 * $(wc -c <"$newest")))
  [ "$required_bytes" -ge "$GIB" ] || required_bytes=$GIB
  basis="twice the newest dump, at least 1 GiB"
else
  required_bytes=$((5 * GIB))
  basis="5 GiB before the first dump"
fi
if [ "$required_bytes" -gt 0 ]; then
  free_before="$(free_bytes_of "$backup_dir")" || fail "cannot read free space of $backup_dir (df failed)"
  if [ "$free_before" -lt "$required_bytes" ]; then
    fail "refusing to start: $(human "$free_before") free in $backup_dir, need $(human "$required_bytes") ($basis). No backup was written or deleted. Add disk space or move old backups to other storage (docs/RUNBOOK.md, 'Disk space for backups'), or set BACKUP_MIN_FREE_MB."
  fi
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/health-$stamp.dump.$method"
partial="$("$PYTHON" -m app.services.operations.backup_files create "$output")" \
  || fail "cannot allocate a private encrypted dump file"

if ! docker compose ${compose_env[@]+"${compose_env[@]}"} --profile backup run --rm -T backup \
  | "$PYTHON" -m app.services.operations.backup_files check-stream \
  | "${encrypt[@]}" >"$partial"; then
  fail "pg_dump failed or was empty, or encryption failed; no backup was written"
fi
[ -s "$partial" ] || fail "encrypted dump is empty; no backup was written"
output="$("$PYTHON" -m app.services.operations.backup_files publish "$partial" "$output")" \
  || fail "cannot publish encrypted dump; existing backups were preserved"
partial=""
size_bytes="$(wc -c <"$output" | tr -d ' ')"

# Opt-in retention: only after a successful new backup, only health-* dumps
# (including legacy plaintext ones). Off by default.
if [ -n "$retention_days" ]; then
  pruned="$(find "$backup_dir" -maxdepth 1 -type f \
    \( -name 'health-*.dump' -o -name 'health-*.dump.age' -o -name 'health-*.dump.gpg' \) \
    ! -path "$output" -mtime "+$retention_days" -print -delete)"
  if [ -n "$pruned" ]; then
    printf '%s\n' "$pruned" >&2
    pruned_count="$(printf '%s\n' "$pruned" | wc -l | tr -d ' ')"
    echo "backup: BACKUP_RETENTION_DAYS=$retention_days deleted $pruned_count old dump(s)" >&2
  fi
fi

free_after="$(free_bytes_of "$backup_dir" || true)"
write_status success "backup written"
echo "backup: wrote $output ($size_bytes bytes); free space in $backup_dir: $(human "${free_after:-0}")" >&2
echo "$output"
