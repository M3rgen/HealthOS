#!/usr/bin/env bash
# Restore a backup into a SEPARATE scratch database and print per-table row counts.
#
#   scripts/restore.sh backups/health-YYYYmmddTHHMMSSZ.dump.age [target_db]
#
# The target (default health_restore) is dropped and recreated; the live `health`
# database is never touched. Decryption keys:
#   *.age  BACKUP_AGE_IDENTITY, else BACKUP_KEY_FILE (default /etc/healthos/backup.key)
#   *.gpg  BACKUP_KEY_FILE passphrase file
#   *.dump legacy plaintext dump
# A restored archive is only usable by the app together with the same
# GOOGLE_TOKEN_ENCRYPTION_KEY that encrypted its OAuth refresh tokens.
set -euo pipefail

# shellcheck source=scripts/backup_env.sh
. "$(dirname "${BASH_SOURCE[0]}")/backup_env.sh"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

fail() {
  echo "restore: $*" >&2
  exit 1
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || {
  echo "usage: restore.sh BACKUP_FILE [target_db]" >&2
  exit 2
}
# The timer's ./backups is mode 700 and root-owned: nothing inside can even be seen without root.
if [ "$(id -u)" != 0 ] && { [ ! -x "$(dirname "$1")" ] || { [ -e "$1" ] && [ ! -r "$1" ]; }; }; then
  echo "restore: $1 is readable only by root; rerunning with sudo." >&2
  command -v sudo >/dev/null || fail "sudo is not installed; rerun this script as root"
  exec sudo ${BACKUP_AGE_IDENTITY:+BACKUP_AGE_IDENTITY="$BACKUP_AGE_IDENTITY"} \
    ${BACKUP_KEY_FILE:+BACKUP_KEY_FILE="$BACKUP_KEY_FILE"} \
    ${BACKUP_ENV_FILE:+BACKUP_ENV_FILE="$BACKUP_ENV_FILE"} \
    bash "$ROOT/scripts/restore.sh" "$@"
fi
backup="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
target="${2:-health_restore}"
[ -f "$backup" ] || fail "no such backup: $1"
[[ "$target" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || fail "target database name must match [a-z_][a-z0-9_]*"
case "$target" in
  health | postgres | template0 | template1) fail "refusing to restore over '$target'; pick a scratch database" ;;
esac
# backup.env (read as backup.sh does) can name a non-default key file.
backup_env_file="${BACKUP_ENV_FILE:-/etc/healthos/backup.env}"
file_key="$(file_value "$backup_env_file" BACKUP_KEY_FILE)"
key_file="${BACKUP_KEY_FILE:-${file_key:-/etc/healthos/backup.key}}"
# Decryption runs after cd to ROOT; keep paths relative to the caller's directory.
case "$key_file" in
  /*) ;;
  *) key_file="$PWD/$key_file" ;;
esac
if [ -n "${BACKUP_AGE_IDENTITY:-}" ]; then
  case "$BACKUP_AGE_IDENTITY" in
    /*) ;;
    *) BACKUP_AGE_IDENTITY="$PWD/$BACKUP_AGE_IDENTITY" ;;
  esac
fi

case "$backup" in
  *.age)
    command -v age >/dev/null || fail "age is required to decrypt $backup"
    identity="${BACKUP_AGE_IDENTITY:-$key_file}"
    [ -r "$identity" ] || fail "age identity $identity is not readable"
    decrypt=(age --decrypt --identity "$identity" "$backup")
    ;;
  *.gpg)
    command -v gpg >/dev/null || fail "gpg is required to decrypt $backup"
    [ -r "$key_file" ] || fail "passphrase file $key_file is not readable"
    decrypt=(gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$key_file"
      --decrypt "$backup")
    ;;
  *.dump) decrypt=(cat "$backup") ;;
  *) fail "unknown backup type: $backup" ;;
esac

cd "$ROOT"
psql_in() {
  docker compose exec -T postgres psql -U health -d "$1" -v ON_ERROR_STOP=1 \
    --tuples-only --no-align --field-separator=$'\t' --command "$2"
}

# The scratch copy shares the live cluster's data volume; check before touching it.
human_gib() {
  case "$1" in
    '' | *[!0-9]*) printf 'unknown GiB' ;;
    *) awk -v b="$1" 'BEGIN { printf "%.2f GiB", b / 1073741824 }' ;;
  esac
}
space_failure() {
  fail "$1; free: $(human_gib "$free_bytes"), live database: $(human_gib "$live_bytes"). See docs/RUNBOOK.md#watch-the-disk"
}
live_bytes="$(psql_in health "SELECT pg_database_size('health')" 2>/dev/null)" || live_bytes=''
free_kib="$(docker compose exec -T postgres df -Pk /var/lib/postgresql/data 2>/dev/null \
  | awk 'END { print $4 }')" || free_kib=''
free_bytes=''
case "$free_kib" in
  '' | *[!0-9]*) space_failure "cannot measure free space; refusing restore" ;;
esac
# Match backup.sh's POSIX df parsing: Available is in 1024-byte blocks.
free_bytes="$((free_kib * 1024))"
case "$live_bytes" in
  '' | *[!0-9]*) space_failure "cannot measure live database size; refusing restore" ;;
esac
[ "$free_bytes" -ge "$live_bytes" ] || space_failure "insufficient free space; refusing restore"

docker compose exec -T postgres dropdb -U health --if-exists "$target"
docker compose exec -T postgres createdb -U health "$target"
"${decrypt[@]}" | docker compose exec -T postgres \
  pg_restore -U health --dbname="$target" --no-owner --no-privileges --single-transaction --exit-on-error

# Inspect only the scratch copy; even an old/incompatible dump never touches live data.
revision="$(psql_in "$target" 'SELECT version_num FROM alembic_version ORDER BY version_num' 2>/dev/null)" \
  || fail "restored revision query failed; schema compatibility cannot be verified"
live_revision="$(psql_in health 'SELECT version_num FROM alembic_version ORDER BY version_num' 2>/dev/null)" \
  || fail "live revision query failed; schema compatibility cannot be verified"
[ -n "$revision" ] && [ -n "$live_revision" ] \
  || fail "missing Alembic revision; refusing to validate a dump with an unknown schema version"
[ "$revision" = "$live_revision" ] \
  || fail "schema version mismatch: restored [$revision], live [$live_revision]. Refusing to validate this dump for live restore. Scratch database '$target' was kept; live data was not changed. Use matching application/migrations in an isolated environment first."

count_sql="SELECT table_name, (xpath('/row/c/text()', query_to_xml(format(
  'SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"

restored="$(psql_in "$target" "$count_sql" 2>/dev/null)" || fail "restored row-count query failed"
# A successful query with no rows means the dump restored nothing; never a valid drill.
[ -n "$restored" ] || fail "restored database $target has no tables"
live="$(psql_in health "$count_sql" 2>/dev/null)" || fail "live row-count query failed"

# Validate both complete query results before producing any comparison. A query
# error or malformed count must never look like an empty/equal archive.
for counts in "$restored" "$live"; do
  [ -n "$counts" ] || continue
  printf '%s\n' "$counts" | awk -F '\t' '
    NF != 2 || $1 == "" || $2 !~ /^[0-9]+$/ || seen[$1]++ { exit 1 }
  ' || fail "invalid row-count query result"
done

echo "Restored $(basename "$backup") into database '$target'."
printf 'table\trestored\tlive\tcomparison\n'
{
  printf '%s\n' "$restored"
  printf '\n'
  printf '%s\n' "$live"
} | awk -F '\t' 'BEGIN { OFS="\t" }
  NF == 0 { side=1; next }
  !side { restored[$1]=$2; tables[$1]=1; next }
  { live[$1]=$2; tables[$1]=1 }
  END {
    for (table in tables) {
      r=(table in restored); l=(table in live)
      if (!r) status="live_only"
      else if (!l) status="restored_only"
      else if (("x" restored[table]) == ("x" live[table])) status="equal"
      else status="different"
      print table, (r ? restored[table] : "-"), (l ? live[table] : "-"), status
    }
  }
' | LC_ALL=C sort
echo "Counts compare a backup snapshot with the current live archive; differences alone do not imply corruption."
echo "Alembic revision: $revision"
echo "Drop the scratch copy when done: docker compose exec -T postgres dropdb -U health $target"
