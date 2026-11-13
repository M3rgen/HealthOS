# Runbook

Routine operations for the deployment in [DEPLOYMENT.md](DEPLOYMENT.md). Commands run on the server in
`/opt/healthos` unless stated otherwise. API calls need the UI token:

```sh
TOKEN="$(sed -n 's/^APP_UI_TOKEN=//p' .env)"
api() { printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl --config - -fsS "http://127.0.0.1:8000$1"; }
```

Before anything that rebuilds or migrates: take a backup (`make backup`; it reads
`/etc/healthos/backup.env` and reruns itself with `sudo` when that file or `backups/` is root-only,
so it may ask for your password) and note the current Alembic revision
(`docker compose exec app alembic current`). Examples assume `APP_PORT=8000`; only
the host side of the published port changes when you set another.

## Update the application

```sh
make backup
git pull --ff-only
docker compose up -d --build        # migrate must exit 0 before app starts
docker compose ps
api /health
```

Recreating the app interrupts the webhook for a few seconds; Google retries failed deliveries.

**After every update, on a host that uses them:**

```sh
# Backup timer: the units live outside the checkout. Needs systemd 247+.
sudo install -m 644 deploy/systemd/healthos-backup.service deploy/systemd/healthos-backup.timer \
  deploy/systemd/healthos-backup-failure.service /etc/systemd/system/
sudo systemctl daemon-reload

# Umay bridge: its project dependencies, then its process.
UV_PYTHON_INSTALL_DIR=/opt/healthos/.python uv sync --frozen --no-dev
sudo systemctl restart healthos-bridge
```

Without the service, stop and relaunch the bridge by hand ([Restart the Umay bridge](#restart-the-umay-bridge)).
The bridge fixes its policy, RPC filter and persona when it starts and they are fingerprinted, so a bridge
left running from older code does not match the updated app: Umay refuses to chat and the log
says `refused: bridge not verified under the current policy; restart the bridge with scripts/start_codex_bridge.py`. The rest of HealthOS
is unaffected.

Updating a deployment that predates the backup metadata mount (`BACKUP_STATUS_HOST_DIR`,
default `./backup-status`): create the directory once before `docker compose up`, so it
starts with the deploy user and is not created by Docker:

```sh
install -d -m 755 backup-status   # or the absolute BACKUP_STATUS_HOST_DIR shared with the timer
```

If it is missing, Compose creates it owned by root (`create_host_path`) and the app still
starts. The backup unit takes both `backups` and `backup-status` for root before every run
([details](DEPLOYMENT.md#8-encrypted-backups)), so no `chown` is needed either way. `GET /health/backup`
reports `missing` until the next backup run.

### Upgrade from v0.2.2

After upgrading from v0.2.2 (to 0.2.2.1 or later), Google sync pauses until you use "Connect Google" once for fresh offline
consent. Pre-upgrade tokens cannot be read even with the same root. Records and backups are preserved.

v0.2.2's Apple extension stamped databases `20261107_a001`; trees without it failed with `Can't locate revision`.
This tree translates that stamp to empty bridge `20261109_p001` before `alembic upgrade head`, then merges at `20261109_0029`.
An installed real Apple revision is never translated or skipped; all `apple_*` tables, rows and guards remain.
This tree declares no Apple schema, so `healthos verify` reports shared protective functions on those unregistered tables;
their names grant no exemption. Verify extension guards with a tree declaring them. Afterwards
`alembic current` prints the newest revision (`20261112_0034 (head)` when this was written).

```sh
make backup
git fetch origin
git checkout <release commit>   # or: git pull --ff-only, if your branch descends from v0.2.2
docker compose up -d --build    # migrate runs 20261107_a001 -> 20261109_0029
docker compose exec app alembic current
docker compose exec app healthos verify
```

Tested on a real v0.2.2 database by replacing the tree at the release commit, not with `git pull`, and
the development branch is not a descendant of the v0.2.2 tag: if `git pull --ff-only` refuses, use the
explicit checkout. The v0.2.2 `.env` worked unchanged. Without `POSTGRES_APP_*` an install that holds
records keeps running as the owner ([Application database role](#application-database-role)); a Umay
bridge must be restarted once ([Restart the Umay bridge](#restart-the-umay-bridge)).

- Before upgrading, check the [application login](#application-database-role): production now
  refuses owner/privileged-role membership (including manual `GRANT health TO healthos_app`)
  and privileged attributes. Compose roles created by `provision-app-role` are unaffected; it
  already rejects these memberships and resets attributes on every `up`.

## Apply migrations

Migrations in `migrations/versions/` run once in the `migrate` service on `compose up`, as the
schema owner; app restarts do not migrate, and they log to `docker compose logs migrate`, not `app`
(`Running upgrade ...`). `migrate` has `restart: "no"`, so a failed attempt is retried only when you
run `docker compose up -d` again. Standalone images can opt into the old behavior with
`HEALTHOS_MIGRATE_ON_START=1`, which needs an owner-role `DATABASE_URL`. To inspect revisions or retry a failed
update (the app's role can read the revision, but only the owner can change the schema, so run
`upgrade` and `downgrade` through `migrate`):

```sh
docker compose exec app alembic current
docker compose exec app alembic heads
docker compose run --rm migrate alembic upgrade head   # or: docker compose up -d migrate && docker compose up -d app
```

If migration fails, app startup is blocked; inspect `docker compose logs migrate`, then retry.
Migrations are additive (no destructive `DROP` outside `downgrade()`); some downgrades cannot fully
undo PostgreSQL enum additions, so roll back with a restore rather than `alembic downgrade` on the
live archive. A downgrade that would lose rows [refuses](MIGRATIONS.md#downgrades-that-would-lose-data-are-refused).

### Migration notes by revision

Notes on every revision from `20261020_0010` on (what it changed, locks, rollback), and on the [refusal of downgrades that lose data](MIGRATIONS.md#downgrades-that-would-lose-data-are-refused), are in [Migration notes](MIGRATIONS.md). Old links to this page still land here:

| Revision and topic | Notes |
| --- | --- |
| <a id="20261020_0010-raw_json-json--jsonb"></a>20261020_0010 (raw_json json → jsonb) | [20261020_0010](MIGRATIONS.md#20261020_0010-raw_json-json--jsonb) |
| <a id="20261020_0011-raw-versions-verbatim-responses-delete-guards"></a>20261020_0011 (raw versions, verbatim responses, delete guards) | [20261020_0011](MIGRATIONS.md#20261020_0011-raw-versions-verbatim-responses-delete-guards) |
| <a id="rolling-back-the-application-after-20261020_0011"></a>Rolling back the application after 20261020_0011 | [Rolling back the application after 20261020_0011](MIGRATIONS.md#rolling-back-the-application-after-20261020_0011) |
| <a id="20261021_0012-record-identity-source-chains-superseded-duplicates"></a>20261021_0012 (record identity, source chains, superseded duplicates) | [20261021_0012](MIGRATIONS.md#20261021_0012-record-identity-source-chains-superseded-duplicates) |
| <a id="20261024_0013-analytics-views-count-what-every-reader-counts"></a>20261024_0013 (analytics views count what every reader counts) | [20261024_0013](MIGRATIONS.md#20261024_0013-analytics-views-count-what-every-reader-counts) |
| <a id="20261024_0014-test-entry-flags-on-manual-measurements"></a>20261024_0014 (test-entry flags on manual measurements) | [20261024_0014](MIGRATIONS.md#20261024_0014-test-entry-flags-on-manual-measurements) |
| <a id="coverage-and-retrieval-receipts--20261025_0015"></a>Coverage and retrieval receipts — 20261025_0015 | [Coverage and retrieval receipts — 20261025_0015](MIGRATIONS.md#coverage-and-retrieval-receipts--20261025_0015) |
| <a id="reconciliation-difference-history--20261025_0016"></a>Reconciliation difference history — 20261025_0016 | [Reconciliation difference history — 20261025_0016](MIGRATIONS.md#reconciliation-difference-history--20261025_0016) |
| <a id="signed-webhook-bodies-jsonb-cannot-hold--20261031_0021"></a>Signed webhook bodies jsonb cannot hold — 20261031_0021 | [Signed webhook bodies jsonb cannot hold — 20261031_0021](MIGRATIONS.md#signed-webhook-bodies-jsonb-cannot-hold--20261031_0021) |
| <a id="google-records-jsonb-cannot-hold--20261102_0023"></a>Google records jsonb cannot hold — 20261102_0023 | [Google records jsonb cannot hold — 20261102_0023](MIGRATIONS.md#google-records-jsonb-cannot-hold--20261102_0023) |
| <a id="deferred-derived-day-invalidation--20261108_0028"></a>Deferred derived-day invalidation — 20261108_0028 | [Deferred derived-day invalidation — 20261108_0028](MIGRATIONS.md#deferred-derived-day-invalidation--20261108_0028) |

Applying a revision needs nothing beyond [Update the application](#update-the-application) unless its notes say otherwise; `20261020_0011` and `20261031_0021` stop after 30 seconds, changing nothing, when a nightly `pg_dump` is running, so update outside the backup window.

## Guards and the database superuser

Production's `health` role (`POSTGRES_USER` of the official image) is a **superuser** and owns
every table. It is used by `migrate`, `provision-app-role` and `backup`; the app itself connects as
the non-superuser role described in [Application database role](#application-database-role) on every
install that sets it. Triggers stop `DELETE`, `TRUNCATE`, `UPDATE` and `session_replication_role = replica`
from any role, but no trigger can stop the owner's or a superuser's DDL: `DROP TABLE`, `ALTER TABLE
... DISABLE TRIGGER`, `DROP TRIGGER`, `CREATE OR REPLACE FUNCTION`, or a trigger recreated with
`WHEN (false)`. `healthos verify` detects every change to the guards (missing, disabled,
redefined, extra triggers, replaced functions) and the backups recover dropped data. The
application role cannot issue that DDL. Without `POSTGRES_APP_*` (older installs that have not opted
in) the app still connects as the owner, and only the triggers, `verify` and backups apply.

If `verify` reports a guard as `disabled` or `enabled (not ALWAYS)` (for example after a
`pg_restore --disable-triggers` data-only restore), re-enable it with the `ALTER TABLE ... ENABLE
ALWAYS TRIGGER ...` statement it prints. A *missing* trigger, a *redefined* one (`was redefined`:
other events, timing, level, columns or `WHEN` than migration 0011 created, e.g. `WHEN (false)`),
an *unexpected trigger on a guarded table* or a *changed* function means the schema was altered by
hand: restore from a backup rather than patching it.

## Import payload versions from archive dumps

Payload versions overwritten before `20261020_0011` survive only in older dumps. Import them one dump at a
time, **newest first** (that order keeps a payload that came back, A → B → A, as three versions): restore into
a scratch database, import, drop the scratch database. The live archive keeps running; each batch of 2,000
records is locked for a moment while its versions are added. A restored dump needs free disk of roughly the
live database's size; check `df -h` first. (With disk for several, restore them all and pass `--from-db` once
per database: the command orders them newest first itself.) Imports run as the owner because the application
role has no table privileges in the scratch database.

```sh
make backup                                          # a fresh backup of the live archive first
for dump in $(ls -r /path/to/encrypted/archive-*.dump.gpg); do   # newest first (timestamped names)
  bash scripts/restore.sh "$dump" health_archive     # decrypts (BACKUP_KEY_FILE) into a scratch DB
  docker compose run --rm --no-deps -T migrate healthos import-versions --from-db health_archive --dry-run </dev/null
  docker compose run --rm --no-deps -T migrate healthos import-versions --from-db health_archive </dev/null || break
  docker compose exec -T postgres dropdb -U health health_archive
done
docker compose exec app healthos verify
docker compose exec app healthos history --data-type heart-rate-variability --no-payload
```

`restore.sh` never touches `health` and decrypts `.gpg` files with the passphrase in
`BACKUP_KEY_FILE` (default `/etc/healthos/backup.key`) and `.age` files with
`BACKUP_AGE_IDENTITY`; the decrypted dump is streamed into `pg_restore` and never written to disk.
Each import prints JSON per source database: `imported` versions added, `already_present`,
`renumbered` existing history rows, `restored_records` (records only the dump had, now rows of
their own marked source-deleted), `source_hash_mismatches` and `unrepresentable` (skipped; the
command exits 1 and the loop stops, so keep that dump file and report the listed keys). A repeated
import adds nothing. Expect about 30 seconds per million dump rows for the scan, plus the
`pg_restore` time.

## Audit what Google sent

Every Google response is in `raw_api_responses` (see
[Verbatim Google responses](reference/data-safety.md#verbatim-google-responses)). To check a record against
the exact page it came from:

```sh
docker compose exec app healthos history --data-type steps --id 'users/me/dataTypes/steps/dataPoints/…' --no-payload
# each version shows response_id; export that page byte for byte:
docker compose exec -T app healthos export-response <response_id> > page.json
sha256sum page.json            # equals body_sha256 in the metadata printed on stderr
docker compose exec app healthos responses --context webhook:<element hash>   # all pages of one run
```

`healthos verify` recomputes every stored body's SHA-256; run it after restores and in the
backup drill.

## Application database role

Runtime access uses `POSTGRES_APP_USER` and `POSTGRES_APP_PASSWORD`; `health` owns the schema.
The provisioned login cannot disable triggers, truncate, alter tables, change `session_replication_role` or use `COPY ... PROGRAM`. Passwords reach PostgreSQL as SCRAM verifiers.
`provision-app-role` runs additively on every `docker compose up`, without changing data.
New databases require both settings; `POSTGRES_ALLOW_OWNER_RUNTIME=1` explicitly overrides this.
Existing records or a `monitoring_started` event identify an earlier install that keeps its owner
login until opt-in. Production refuses superusers (`DATABASE_ROLE_SUPERUSER_REFUSED`).
A direct non-superuser owner stays warning-only: `DATABASE_ROLE_UNRESTRICTED`,
`database_role_schema_owner`; status is unchanged.
Production also refuses direct or indirect membership of an owner or privileged role, regardless
of `INHERIT`, and logins with `CREATEROLE`, `CREATEDB`, `REPLICATION` or `BYPASSRLS`
(`DATABASE_ROLE_PRIVILEGED_REFUSED`). Fix memberships with `REVOKE <role> FROM <login>;` or
attributes with `ALTER ROLE <login> NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;`, then
run `docker compose up -d`; revoke the membership leading to an indirect dangerous role too.
`HEALTHOS_ALLOW_SUPERUSER_UNTIL=<YYYY-MM-DD>` accepts either risk through that date, at most 30
days ahead. Warnings: `DATABASE_ROLE_SUPERUSER_GRACE` or `DATABASE_ROLE_PRIVILEGED_GRACE`.
`/health` reasons: `database_role_superuser` and `database_role_superuser_grace`, or
`database_role_privileged_role` and `database_role_privileged_role_grace`.
Before upgrading, run this startup policy as the application login in `psql`; expect no rows:
```sql
SELECT r.rolname FROM pg_roles r WHERE r.rolname = current_user AND (
  r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls
  OR EXISTS (SELECT 1 FROM pg_roles b WHERE b.rolname <> current_user
    AND pg_has_role(current_user, b.oid, 'MEMBER') AND (
      b.rolname = (SELECT pg_get_userbyid(relowner) FROM pg_class
                  WHERE oid = to_regclass('public.raw_health_data'))
      OR b.rolsuper OR b.rolcreaterole OR b.rolcreatedb OR b.rolreplication OR b.rolbypassrls
      OR b.rolname IN ('pg_execute_server_program', 'pg_read_server_files',
                      'pg_write_server_files', 'pg_database_owner')
      OR b.oid IN (SELECT relowner FROM pg_class WHERE relnamespace = 'public'::regnamespace))));
```
After a backup, add the settings below to `.env`, then run `docker compose up -d`. Revert by removing both and setting the dated exception (an empty new database also needs `POSTGRES_ALLOW_OWNER_RUNTIME=1`); the role remains. Rotate the password in `.env` and rerun `docker compose up -d`. After restoring a backup, reapply grants before starting: `docker compose run --rm --no-deps provision-app-role`.
```sh
pw="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf 'POSTGRES_APP_USER=healthos_app\nPOSTGRES_APP_PASSWORD=%s\n' "$pw" >> .env
```

## Update images and cloudflared

Base images are pinned by tag and digest; follow [IMAGE_UPDATES.md](IMAGE_UPDATES.md) and change one
component at a time. Never change the PostgreSQL major version by editing the tag.

`cloudflared` runs with `--no-autoupdate`. To update: download the new release for the server's
architecture, verify its checksum, replace `/usr/local/bin/cloudflared`, then
`sudo systemctl restart cloudflared` and repeat the public-surface checks in
[CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md#verify-the-public-surface).

## Rotate secrets

| Secret | Supported? | Procedure |
| --- | --- | --- |
| `APP_UI_TOKEN` | Yes | Put a new value in `.env`, then `docker compose up -d --force-recreate app`. The restart ends every login session (sessions are kept in memory); log in again. |
| `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` | Yes, with a gap | The app accepts exactly one value; there is no overlap period. Update the subscriber's authorization with Google and `.env` back to back, then recreate the app. Deliveries in between get 401; Google retries them and reconciliation covers anything lost. Run `healthos webhook-readiness` afterwards. |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | Yes, requires consent | Take a backup. Generate a new Fernet-format root, set it as `GOOGLE_TOKEN_ENCRYPTION_KEY`, then `docker compose up -d --force-recreate app`. The stored Google connection becomes unreadable; use "Connect Google" once to grant fresh offline consent. Retired roots are never read, even when listed in `GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD`. |
| `CODEX_BRIDGE_TOKEN` | Yes | Stop the bridge (below). For systemd, change the private service environment and app `.env` to the same new value, then restart the service and recreate the app. Manual mode: remove the old token file and configured token, rerun the launcher, then recreate the app. |
| `POSTGRES_PASSWORD` | Manual | The image only reads it when the data directory is first created. Change the role first, `docker compose exec postgres psql -U health -d health -c '\password health'`, then update `.env` and run `docker compose up -d` (the app needs it only without `POSTGRES_APP_*`). |
| `POSTGRES_APP_PASSWORD` | Yes | Change it in `.env`, then `docker compose up -d`: `provision-app-role` sets the new password and the app is recreated with it. |
| Backup key | Yes | Point `BACKUP_AGE_RECIPIENT` (or `/etc/healthos/backup.key`) at the new key. New backups use it. Backups are kept forever, so keep the old identity (in the password manager) for as long as any backup made with it exists. |

After reconnecting, verify that Google sync succeeds. `GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD` is validated
but never reads tokens. Restart pending OAuth states after rotation. Records, credential versions and backups remain; keep archived roots securely.

### If a root key or database dump leaks

Revoke HealthOS access on your Google account's third-party access page, generate and configure a
new root as above, then use "Connect Google" to grant fresh offline consent. Old refresh tokens
remain in append-only credential history and backups: treat them as valid until revoked at Google.

## Backup, verify and restore drill

```sh
make backup                          # prints backups/health-<stamp>.dump.age
sudo ls -l backups/                  # sudo: after a timer run the directory is root's
sudo cat backups/last-backup-status.json  # detailed sizes; may be stale after a write failure
sudo cat backup-status/status.json   # "last_attempt" and "last_success_at" read by the app
systemctl list-timers healthos-backup.timer
sudo journalctl -u healthos-backup.service -n 20
sudo journalctl -p crit -t healthos-backup   # failures reported by healthos-backup-failure.service
```

Each run streams ciphertext into its own private temporary file. A stdlib Python helper
publishes the complete dump with an atomic hard link that cannot replace an existing name;
same-second collisions add a unique suffix before `.dump.age` or `.dump.gpg`. The backup
filesystem must support hard links, otherwise publication fails and old backups stay intact.
Discovery, restore and explicitly enabled retention accept both naming forms.

The timer runs daily at 03:30 UTC, with up to five minutes of random delay, regardless of the
host's time zone. Check backup status weekly and after any failure: start with the protected
`/health` (`backup_failed`, `backup_missing`, `backup_status_unreadable`) and
`backup-status/status.json` (`last_attempt.result`, `last_attempt.timestamp`, `last_success_at`).
The metadata directory is `BACKUP_STATUS_HOST_DIR` (default `/opt/healthos/backup-status`),
mounted at `/run/healthos-backup`. Use `sudo journalctl` above for the failure reason; an account
outside `adm`/`systemd-journal` may otherwise see no entries.

Detailed `BACKUP_HOST_DIR/last-backup-status.json` includes `free_bytes` and a failure `message`
when it can be written. If the backup disk is unwritable, it can retain an old `success`:
never use that result alone to decide that the latest attempt succeeded. If the unit fails before
the script starts, even `status.json` may be stale; compare its time with the journal and timer.

The protected `GET /health/backup` reads only status metadata, without database access.
Its `status` is `available`, `legacy`, `missing`, `invalid`, `unreadable` or `unconfigured`.
`last_attempt` contains `result` (`success`/`failure`) and UTC `timestamp`;
`last_success_at` is UTC or `null` when there is no evidence. A failure preserves the
previous success across app recreation. Success is published only after encryption
and final artifact placement. No paths, keys or raw error messages enter this response.
The old detailed `last-backup-status.json` remains compatible. On first publication,
only a valid legacy **success** proves an earlier success; a legacy failure cannot.
Back up the metadata directory along with operational state to retain that history.
`/health` repeats `last_success_at` as `last_backup_success`, which the Sistem page shows as
**Son yedek**, and lists `backup_missing`, `backup_failed` or `backup_status_unreadable` among its
`reasons` without changing `status` ([Local API](reference/api.md#local-api)).

### Watch the disk

Nothing here ever deletes data to save space: every Google response (`raw_api_responses`), every
payload version and every backup dump is kept forever, so the disk only fills. There is no
`healthos` command for disk usage; check monthly, and before imports or migrations:

```sh
df -h /var/lib/docker backups/                    # database volume; backups (or BACKUP_HOST_DIR)
docker compose exec -T postgres psql -U health -d health -c "SELECT pg_size_pretty(pg_database_size('health'))"
docker compose exec -T postgres psql -U health -d health -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class
   WHERE relname IN ('raw_api_responses', 'raw_health_data', 'raw_health_data_versions')"
sudo du -sh backups/                              # or BACKUP_HOST_DIR
sudo cat backups/last-backup-status.json          # size_bytes, free_bytes, required_free_bytes
```

When free space trends towards the backup guard (`required_free_bytes`, twice the newest dump),
grow the disk or move old dumps as below; never delete data or responses to make room.

### Disk space for backups

Backups are kept forever and nothing deletes them automatically. When free space falls below the
guard (twice the newest dump, at least 1 GiB; or `BACKUP_MIN_FREE_MB`), the backup refuses to start
and the unit fails; it never deletes old backups to make room. Fix it by adding space:

- **Grow the disk.** Enlarge the server disk in the hosting environment, then grow the partition and filesystem
  (e.g. `sudo growpart /dev/sda 1 && sudo resize2fs /dev/sda1`; check names with `lsblk`).
- **Add a disk for backups.** Format and mount a new disk (e.g. at `/srv/healthos-backups`, listed
  in `/etc/fstab`), copy the existing backups there (`sudo rsync -a backups/ /srv/healthos-backups/`),
  and set `BACKUP_HOST_DIR=/srv/healthos-backups` in `/etc/healthos/backup.env` (edit the existing
  file with `sudoedit`; keep its key settings and mode 600). Use that file for this setting in
  both manual and timer runs, as in [deployment](DEPLOYMENT.md#8-encrypted-backups).
  The systemd sandbox must follow the new directory: install the shipped drop-in, which replaces
  the unit's write access and ownership preparation with the new disk and the metadata directory:

  ```sh
  sudo install -d -m 755 /etc/systemd/system/healthos-backup.service.d
  sudo install -m 644 deploy/systemd/healthos-backup-paths.conf.example \
    /etc/systemd/system/healthos-backup.service.d/backup-paths.conf
  sudo systemctl daemon-reload
  make backup
  sudo systemctl start healthos-backup.service
  sudo journalctl -u healthos-backup.service -n 20
  sudo cat backup-status/status.json
  ```

  For another disk, checkout or `BACKUP_STATUS_HOST_DIR`, edit the installed file before
  `daemon-reload` and replace every `/srv/healthos-backups` and `/opt/healthos/backup-status`.
  Its empty `ExecStartPre=` and `ReadWritePaths=` lines reset the shipped lists, so the unit no
  longer prepares or writes the old backup directory; keep the metadata directory separate from
  the backups, and leave `ProtectSystem=strict` and the empty capability set as they are.
  The `|| echo` tolerates an inaccessible `lost+found` on a mounted disk; the backup still fails
  if its destination is unwritable. A manual backup alone does not test the timer's sandbox.
  Check `/health` and the metadata's latest attempt after the unit run, then perform a restore
  drill from the new disk. Keep the old copies until that drill succeeds.
- **Move old backups to other storage, by hand.** They are encrypted, so any storage will do. Copy,
  verify, and only then remove the local copy:

  ```sh
  rsync -a --checksum backups/health-2026*.dump.age /mnt/archive/healthos/
  (cd backups && sha256sum health-2026*.dump.age) > /tmp/healthos-moved.sha256
  (cd /mnt/archive/healthos && sha256sum -c /tmp/healthos-moved.sha256)   # all OK before deleting
  ```

  Keep at least the newest backups on the host, and keep the backup key with the archive.

Restore drill, monthly and after every change to backups or keys. It never touches the live
`health` database: it drops and recreates a scratch database, restores into it, and prints row
counts across the union of tables plus the restored Alembic revision. Each table is
`equal`, `different`, `restored_only` or `live_only`; query/invalid-output errors and a
restored copy without tables fail the command instead of appearing equal. Count differences are informational: an old
snapshot and a changing live archive normally differ, and equal counts alone do not
prove content integrity. The isolated Compose drill freezes synthetic data and fails
unless every table matches. No comparison restores into the live database. The drill requires free space equal to the live database size on the PostgreSQL data volume, which the script checks before touching the scratch database.

```sh
BACKUP_AGE_IDENTITY=/path/to/healthos-backup.age \
  make restore FILE=backups/health-<stamp>.dump.age          # default target: health_restore
docker compose exec -T postgres dropdb -U health health_restore
docker compose exec app healthos verify                 # live archive hash check
```

If the age identity lives only off the server (recommended), copy it in for the drill and remove it
afterwards. Replacing the live database is a separate, destructive procedure; see the
[backup reference](reference/data-safety.md#database-backup-and-restore).

## Restart the Umay bridge

The host bridge can use [`deploy/systemd/healthos-bridge.service`](../deploy/systemd/healthos-bridge.service). Paths assume `/opt/healthos`, dependencies installed with `UV_PYTHON_INSTALL_DIR=/opt/healthos/.python uv sync --frozen --no-dev`, and CLI `/usr/local/bin/codex`.
Install **Codex CLI 0.161.0 exactly**: the full platform package archive with companion executables from the official release page, or npm package `@openai/codex@0.161.0` (`npm install -g @openai/codex@0.161.0`); see [deployment step 9.1](DEPLOYMENT.md#9-optional-the-umay-bridge).
The single-binary `codex-x86_64-unknown-linux-musl.tar.gz` omits `codex-code-mode-host`. Without it, startup refuses the CLI: `exec did not report its nested tools; codex-code-mode-host may be missing: install the full Codex package`.
Check `ls -l /usr/local/bin/codex /usr/local/bin/codex-code-mode-host`: both must be executable, side by side. Keep all companion executables together. For npm, check these names with `ls -l` in the package's native binary directory, not beside `codex.js`; retain the full package.
Adjust the unit's explicit paths for your installation (for npm, `CODEX_BRIDGE_CLI` must name the native `codex` binary); no dependency upgrade is needed.

1. Stop any manually launched bridge using its recorded `bridge.pid` before enabling the unit.
   Provision a dedicated account and private home (once):

   ```sh
   sudo useradd --system --user-group --home-dir /var/lib/healthos-bridge --shell /usr/sbin/nologin healthos-bridge
   sudo install -d -m 700 -o healthos-bridge -g healthos-bridge /var/lib/healthos-bridge
   sudo install -d -m 700 /etc/healthos
   ```

2. Sign in on the headless server as this account with the unit's `HOME` and `CODEX_HOME`; the file-store override ensures the launcher finds `$CODEX_HOME/auth.json`.
   Complete the code entry in another device's browser; if disabled, enable device code login in your ChatGPT account's security settings (or ask the workspace admin).
   An existing login can instead be provisioned privately as `/var/lib/healthos-bridge/auth.json`, owner `healthos-bridge`, mode 0600; never put credentials in the checkout or the unit:

   ```sh
   sudo -u healthos-bridge env HOME=/var/lib/healthos-bridge CODEX_HOME=/var/lib/healthos-bridge /usr/local/bin/codex login -c cli_auth_credentials_store=file --device-auth
   sudo -u healthos-bridge env HOME=/var/lib/healthos-bridge CODEX_HOME=/var/lib/healthos-bridge /usr/local/bin/codex login -c cli_auth_credentials_store=file status
   sudoedit /etc/healthos/bridge.env
   sudo chmod 600 /etc/healthos/bridge.env
   ```

   In that root-owned environment file, set `CODEX_BRIDGE_TOKEN` to the backend's existing
   private token (or provision one random value in both places). Do not paste it into shell
   arguments or logs. The service also reuses an existing private `bridge.token` when no
   environment token is supplied; if neither exists, startup fails instead of rotating it.
   Set `CODEX_BRIDGE_BIND_HOST` only to override address selection. The default remains the
   `docker0` gateway on Linux Docker Engine, loopback elsewhere. Only loopback, `docker0` and Compose
   `br-*` bridge addresses are accepted; another address needs `CODEX_BRIDGE_ALLOW_NON_LOCAL_BIND=1`
   (a warning at every start; restrict port 8766 with the firewall), and `0.0.0.0` is always refused.
   A token that only passes the earlier 16-character strength rule is refused unless
   `HEALTHOS_ALLOW_LEGACY_SECRETS=1` is set ([strength](reference/config.md#secret-strength)). The app's existing
   `host.docker.internal:host-gateway` mapping needs no change. Keep the backend's explicit
   `CODEX_BRIDGE_URL`; for a native backend use `wss://127.0.0.1:8766` and a loopback bind.
   Service mode never writes `.env` and does not need to read it: the checkout's mode 600
   `.env` stays unreadable to this account, so service settings belong in `bridge.env`.
   Recreate the app only after a token or URL change.

3. Apply the existing [`deploy/firewall/`](../deploy/firewall/) nftables or UFW instructions
   before starting. Only loopback and Docker bridges may reach port 8766. UI/API stays on
   `127.0.0.1`; the public tunnel still allows only `POST /webhooks/google-health`.
   The account needs read/execute access to the checkout and Python environment, not write access.
   The virtualenv's Python interpreter and CLI must live outside `/home` (`ProtectHome=true`). If
   `uv` downloaded Python into your home directory, the unit's `ExecStartPre=` check stops it with
   a journal line naming the fix instead of a bare `203/EXEC`; rebuild the environment inside the
   checkout:

   ```sh
   cd /opt/healthos && rm -rf .venv && UV_PYTHON_INSTALL_DIR=/opt/healthos/.python uv sync --frozen --no-dev
   ```

   ```sh
   sudo install -m 644 deploy/systemd/healthos-bridge.service /etc/systemd/system/healthos-bridge.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now healthos-bridge
   ```

The bridge terminates TLS itself: port 8766 is `scripts/codex_bridge_front.py`, which holds the key
derived from the token, checks the bearer, proves the app-server it forwards to by asking it for the
launcher's random directory, and replaces the bearer with a throw-away inner token. The app-server
listens on a private loopback port. An old plain bridge left running is refused with a clear message:
stop it and start the launcher or the service again, with `.env` unchanged.

The WebSocket ends at the front, which passes only the chat's seven RPCs in their exact shape
([list](ARCHITECTURE.md#local-codex-chat-umay-bridge)). Restart the bridge after every update and
whenever `app/personas/umay/soul.md`, the filter or the chat's thread definition changes: the front
reads them once at start, and the launcher and the chat both refuse a bridge verified under
another version. A manual launcher run says so and tells you which PID to stop.

Routine operations and diagnostics:

```sh
sudo systemctl stop healthos-bridge
sudo systemctl start healthos-bridge
sudo systemctl restart healthos-bridge
sudo systemctl status healthos-bridge --no-pager
sudo journalctl -u healthos-bridge -n 80 --no-pager
ss -ltn | grep 8766
```

`Type=notify` makes `systemctl start` wait for the runtime checks and an authenticated TLS front response before the launcher sends `READY=1`.
After a successful start, open Umay on the local dashboard. The supervisor stops the process group, waits up to five seconds,
then kills leftovers; systemd also owns the entire cgroup with a 15-second stop limit.
Unexpected exits fail the service, including a spontaneous exit 0. Restarts wait 10 seconds,
with at most five starts per 300 seconds. After fixing a login, executable, port conflict or
configuration problem, use `sudo systemctl reset-failed healthos-bridge`, then start it again.
Identity stays in `/var/lib/healthos-bridge/healthos-bridge/`, directory 0700 and private files
0600. Service output goes to the journal; no detached PID is adopted. Enablement survives reboot.
The first service start copies the source login; later starts keep credentials refreshed inside the isolated home.
To renew that login, stop the service and repeat the login command above with `CODEX_HOME=/var/lib/healthos-bridge/healthos-bridge`, then start the service.

Without systemd, the manual launcher remains available; its files are under
`~/.codex/healthos-bridge/` (or `CODEX_HOME`), including `bridge.pid` and `bridge.log`:

```sh
kill "$(cat ~/.codex/healthos-bridge/bridge.pid)"
uv run --frozen --no-dev python scripts/start_codex_bridge.py
docker compose up -d --force-recreate app      # only if token or URL changed
```

Older bridges might still listen on `0.0.0.0`: stop them explicitly before relaunching, since
manual mode reuses a healthy instance. Manual mode alone does not restart after reboot.
In the panel, missing settings, refusal, timeout and disconnect show safe Umay messages;
five retries use increasing waits, then **Yeniden bağlan** resets the budget. Sending is disabled
while waiting. Reconnecting starts a new ephemeral conversation; there is no persistent memory.
Requested health records still go to OpenAI through the signed-in account for interpretation.
Synthetic process/WebSocket tests do not validate server systemd, Docker routing or firewall rules;
the maintainer must verify start/stop/reboot, allowed Docker access and denied LAN access there.

## The webhook went quiet

**Sistem → Durum → Google bildirimleri → Bildirim ayrıntıları** shows the same diagnostics as the
read-only endpoint below (it needs UI authentication and cannot be reached from the public webhook host):

```sh
api /api/webhooks/status | python3 -m json.tool
```

- `last_notification_at`: the last delivery received with a valid signature and at least one
  valid notification element, not counted as an old replay. It is neither processing success
  nor the latest data write. In the older archive, only the verification time of a delivery that
  has its raw bytes and a valid element counts as evidence.
- `silence`: the service computes the seven-day threshold. The first configured application
  start begins the waiting period; a new installation is not immediately treated as silent. An
  unconfigured receiver, no delivery received yet, the threshold being exceeded and the status
  endpoint being unreachable are shown separately in the panel. A challenge, malformed traffic,
  a body whose elements were all rejected and an old replay do not reset the time.
- `counters.scope=persistent_diagnostic_events`: events are only added to the separate
  `webhook_status_events` table added by `20261025_0017`; the webhook archive is not changed.
  `since` is the time of the first diagnostic observation; the counters are shared across
  processes and survive a restart. No counters are derived from the earlier archive; during a
  database outage observations can be missed.
- `reasons` separates authorization, missing/malformed/invalid signature, key download error,
  challenge, duplicate delivery, old replay and processing errors. `units` states the counting
  units. One request can produce more than one event; `valid` is cryptographic success only.
  `invalid` sums missing/malformed/invalid signatures; authorization and key errors stay separate.
  If Google's keyset cannot be fetched (`key_download_failed`), the response is `503`, not `401`:
  it is no verdict on the signature, and Google retries the delivery later.
- Pre-authorization rejections (`authorization_rejected`, `receiver_unconfigured`) can be triggered
  by anyone and their diagnostic rows are not deleted; therefore at most one row per reason per
  minute is persisted. The `counters` value of these two reasons can be lower than the number of
  requests. Reasons at the authorized stage and deliveries are written completely.
- `process_local` is held in memory only since the start of this process (`since`); it resets on
  restart and is not shared between workers. `unpersisted` counts, per reason, the
  pre-authorization rejection rows skipped by sampling. `rate_limited.authorized` and
  `rate_limited.unauthorized` count the requests the two webhook budgets rejected with `429`;
  rate rejections are never persisted ([limits](#webhook-and-manual-write-limits)).
- `expired` is not the signature's expiry: the format has no such field. The local
  `SIGNED_REPLAY_MAX_AGE` rule prevents reprocessing an archived body whose first verification was
  **more than 24 hours** ago. Such a request can count as both `valid` and `expired`;
  the response stays 204. A delivery at exactly 24 hours is accepted under the current rule.
- Because there is no subscription and renewal evidence or client, `subscription.status` and
  `renewal_status` are always `unknown`; configuration readiness is not renewal success.
  The GET call does not go to Google and neither creates nor renews a subscription. `last_error`
  contains only a fixed code, the verification/processing stage and a time; it contains no
  signature, body or health value.

Symptoms: the dashboard's newest records lag by hours, or `/health` reports `stale`/`degraded`.

1. **App state.**

   ```sh
   api /health | python3 -m json.tool
   docker compose exec -T app healthos webhook-status
   ```

   Look at `last_webhook_received`, `last_webhook_processed`, `pending_notifications`,
   `failed_notifications`, `stale_streams` and `reasons`. `stale` means every frequent stream
   (heart rate, steps, activity, energy) is older than `PIPELINE_STALE_THRESHOLD_MINUTES`.
   Received but not processed points at the app or Google API; not received points at the path
   from Google.
2. **Logs.**

   ```sh
   docker compose logs --since 24h app | grep -E 'WEBHOOK_|WATCHDOG_|RECONCILIATION_|TOKEN_REFRESHED|RATE_LIMITED'
   ```

   `WEBHOOK_SIGNATURE_INVALID` with `reason=authorization` means the subscriber's secret and `.env`
   differ. `WEBHOOK_VERIFICATION_UNAVAILABLE` means Google's public keyset could not be fetched:
   the delivery got 503 and Google retries it, so check the app's outbound network rather than the
   secret. A rising `process_local.rate_limited.authorized` means real deliveries are hitting 429
   (`WEBHOOK_REQUESTS_PER_MINUTE`). `WEBHOOK_REPLAY_IGNORED` means a signed body older than 24 hours arrived again and was
   deliberately not reprocessed.
3. **Tunnel.** `systemctl status cloudflared`, `journalctl -u cloudflared --since '6 hours ago'`,
   then the external checks in [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md#verify-the-public-surface):
   an unauthenticated POST must return 401.
4. **Google side.** Check that the subscriber still exists and points at the same URI. Testing-mode
   OAuth refresh tokens expire after seven days, which stops both webhook follow-up reads and syncs.
5. **Recover.** Retry stored but unprocessed notifications, then fill the gap from Google:

   ```sh
   docker compose exec -T app healthos webhook-replay-pending
   docker compose exec -T app healthos reconcile --days 7
   ```

   The watchdog (every `WATCHDOG_INTERVAL_SECONDS`, default 15 minutes) also starts one bounded
   reconciliation by itself when all frequent streams are stale, at most once per
   `RECOVERY_COOLDOWN_MINUTES`. Reconciliation runs daily (7 days back) and weekly (90 days back)
   regardless.

## Abandoned backfill windows

Backfill downloads history in windows (`BACKFILL_WINDOW_DAYS`, default 31) per data type. A window
that fails five times is retried only after every other window; on its tenth failure it is marked
`abandoned` and the app logs `BACKFILL_WINDOW_ABANDONED`. An abandoned window is skipped from then
on, so it no longer blocks newer windows of the same type, but **its date range is missing from the
archive** until you act.

```sh
api /backfill/status | python3 -m json.tool    # rows with "status": "abandoned" and last_error
docker compose exec app healthos backfill  # also lists abandoned windows and exits 1
```

`last_error` holds a short error class and code (for example a 403 for one range). Fix the cause
first, then requeue the abandoned windows with a fresh retry count and run the backfill again in
one step (leave out `--data-type` to requeue every type's abandoned windows):

```sh
docker compose exec app healthos backfill --reset-abandoned --data-type steps
```

A requeued window resumes from its saved page token; if it keeps failing it is abandoned again
after ten more attempts.

If Google simply has no accessible data for that range, leaving the window abandoned is harmless.

### Re-fetching a period whose windows are already completed

A plain `healthos backfill` never re-reads a range whose windows are `completed`; it only
plans before the first and after the last window. To fetch such a period again with retrieval
receipts, add new `pending` windows for it and run `backfill --data-type`. A one-off backfill of a
past period (prerequisites, window SQL, monitoring, stop/resume, verification) follows a separate
maintainer runbook that is not part of the published tree.

## Identity review and decisions

The steps below follow `healthos identity-backfill` (the `identity-reconcile` job runs it nightly). What the migration that introduced it changed and how to roll that back are in the [migration notes](MIGRATIONS.md#20261021_0012-record-identity-source-chains-superseded-duplicates).

Review the dry run before writing anything. Per data type:

- `chains`: every link it would record, `from` → `to` (labels: platform / device or package /
  recording method; `fingerprints`: the first 12 characters of each source's fingerprint) with
  `gap_seconds` between the old form's last and the new form's first sighting. Expect one chain
  per device rename or package-suffix change you know of, in each data type that device writes.
  A chain between two devices you know are different (a band replaced by a watch of the same
  platform, worn one after the other) is wrong: reject it after the run
  (`identity-decide reject`, below); it only ever merges rows with equal readings, so nothing is
  lost meanwhile.
- `review`: the chains decided by the reconciler that you have not confirmed or rejected yet
  (all of them, after the first run). `healthos status` lists them too
  (`identity_review_suggested`) until you decide.
- `ambiguous`: sources the rule left open, each with `why` and the `candidates`: `a source of its
  class ended less than five minutes before` (a rename in the middle of a fetch), `more than one
  source of its class ended before it`, `another source of its class appeared in between`,
  `its predecessor has more than one successor candidate`, `another source of its class was seen
  alongside` / `seen alongside another source of its class` (a second device of the same platform
  and recording method around the rename, not yet known to be another device), `history
  incomplete`. **Nothing is linked for them**: both sources keep their rows, the readings are
  visible (counted twice where they are the same records), and deletion marks between them stay
  deferred. That is the safe side; decide each with `identity-decide`.
- `kept_apart`: pairs of sources of one class that stay separate; `established: true` when
  Google returned different values for the same record under both, for three records in at least
  two fetches, the later one with records newer than the first (two devices recording side by
  side: your second watch belongs here, against both names of the band), or you decided them
  distinct; `false` when nothing decided them (see `ambiguous`).
- `review_distinct`: pairs established distinct by that evidence alone although it looks like a
  rename: one source began as the other ended (and no form of one was worn alongside a form of the
  other for more than a day), or most records both hold came back with the same reading (Google
  reprocessed old records under the new name, a few with other values). Nothing is merged and no
  deletion is marked because of them; confirm with `identity-decide distinct`, or, if it was a
  rename, decide `not-distinct` and `link`.
- A device field Google adds to a source's records (`formFactor`, `manufacturer`: e.g. a band
  sent until then by its name alone, now with `formFactor: "FITNESS_BAND"`, sometimes without the
  name) makes a new form of that source, like a rename: it appears under `chains`/`review` (or
  `ambiguous` when Google sends both forms side by side), and is decided the same way. Two
  different values of such a field (a phone and a watch) are never one device, through whatever
  forms: `identity-decide` refuses a link that would make them one (`ambiguous` says so: "its
  earlier form belongs to a device with another value of a device field").
  Two forms are only taken for one source when every other `dataSource` field is byte for byte
  the same (platform, recording method, the whole `application`, any field Google adds outside
  `device`). If a dry run (`identity-backfill --dry-run`) after Google added a device field shows
  the old form's rows under `deletions_kept` instead of a chain, `ambiguous` or
  `deletions_suspicious`, Google changed another field as well, and the two forms are treated
  as two sources (`identity-decide` refuses to link them, and their deletions are marked like
  any other's): compare the two forms' `dataSource` in what Google sent
  ([Audit what Google sent](#audit-what-google-sent)) and report the difference (its shape, no
  values) so the rule can be extended.
- `deferred_deletions`: rows complete fetches did not mark because another source, that may be
  their device renamed, returned the same reading (`rows_of`, `returned_by`, how many, since
  when; one row each in `identity_deferred_deletions` until a later complete fetch of their
  window decides them).
  Decide that pair: `distinct` (two devices: the next complete fetch marks them) or `not-distinct`
  and `link` (one device: they stay, counted once; the pair leaves this list at once, and the next
  complete fetch of the data type resolves its rows).
- `refuted`: chains whose two forms turned out to be two devices (no longer used: the rows they
  linked count on their own again); a chain in state `lapsed` was decided by the reconciler only
  because another chain made a rival source continue elsewhere, and that chain is no longer live
  (it is live again when that one is); `returned`: chains whose old form was seen again (a name
  changed back: still one device, its records get rows of their own, linked to the device's).
- `linked` (rows that would be superseded, by reason) should be in the order of the chains above;
  `superseded_marked` of those carried a deletion mark (it stays, as history); `deletions_kept`
  the deleted records with no other source at their instant (genuine deletions; expect a few per
  data type), `deletions_suspicious` deleted records whose instant another, undecided source still
  holds live (possibly a rename nobody decided: look at `ambiguous`); `kept_differing` rows of a
  chained source that measured something else than the row holding the record (kept, both
  count); `distinct_sources` the records (match keys) held by two devices; `conflicts` (rows of
  one device left double because linking them would close a cycle of links, or would join two
  values of a device field; listed in `status`) should be 0;
  `start_mismatches` rows with a stale stored start (settled last); `agreement` per pair of
  sources with evidence of two devices, how many records this run settled that both hold with
  the same reading and how many with two (the counts over every record both hold, kept per
  record in `raw_source_agreements`, decide whether the pair is two devices; a chain is refuted
  only when all of them were counted).

Then:

```sh
docker compose exec -T app healthos identity-backfill > /tmp/identity.json
docker compose exec app healthos verify     # exit 0: guards, keys, links, chains, views
docker compose exec app healthos status     # identity_reconcile per data type; review list
docker compose logs app | grep SCHEDULER_STARTED # identity_reconcile_enabled: true
api /health
```

**Deciding what the reconciler left open** (`healthos identity-decide`). Each decision is
recorded append-only in `raw_source_decisions` (with the time and your `--note`), applied at once,
and `verify` checks that every chain you linked names its decision. Always run it with
`--dry-run` first: it writes nothing and prints the effect (links it would make, chains, open
cases afterwards). Fingerprints are given as the reports print them (12 characters; any unique
prefix of at least 6); `healthos identity-sources --data-type <type>` lists every source of a
data type with its fingerprint, sightings, device, the sources it is established distinct from,
the chains with their review state and what the rule leaves open.

```sh
# A rename the rule left open (e.g. within five minutes): FROM is the earlier form.
docker compose exec app healthos identity-decide link --data-type <type> \
    --from <fingerprint> --to <fingerprint> --note "renamed in the app" --dry-run
# A chain you checked: off the review list. A wrong one: no longer used.
docker compose exec app healthos identity-decide confirm --data-type <type> --from … --to …
docker compose exec app healthos identity-decide reject  --data-type <type> --from … --to …
# Two devices whatever the evidence, or a stray duplicate that is not a second device.
docker compose exec app healthos identity-decide distinct     --data-type <type> --from … --to …
docker compose exec app healthos identity-decide not-distinct --data-type <type> --from … --to …
```

Every decision settles again exactly the rows whose devices it changes: after `link`,
`not-distinct` or a `confirm` that makes a rejected chain live again, every row written since the
later of the two sources was first seen (rows of both forms are linked where they hold the same
reading); after `reject` or `distinct`, every row of a link between the two sides (also rows
archived before 20261020_0011). The links made across the two sources stop hiding anything at once
(both count again in the dashboard, Umay and the `analytics_*` views, each device once; `verify`
notes the links as no longer in force), and both sources are free for another chain - so after a
wrong `link`, `reject` it and `link` the right pair. The latest decision per question wins, so
every decision is undone by the opposite one, exactly in what is counted (`link` or `confirm`
after `reject` uses the chain again, and a chain the reconciler decided because of it is live again
with it; `not-distinct` after `distinct`). There is no decision that forgets one: return to "what
the evidence says" by deciding what it says. **`distinct` has a permanent consequence**: from then
on a row of one source whose reading comes back only under the other is marked deleted by the next
complete fetch, and a mark is never removed by a decision (the outcome and `--dry-run` say so in
capitals); undoing it with `not-distinct` restores what is counted, not the marks. Decide it only
for two devices. An owner's `distinct` reaches the other forms of a device only through chains you
linked or confirmed (a chain only the reconciler suggested may be wrong). A decision that would
make one source continue as two (or close a cycle) is refused, naming the chain to reject first.
A deletion left undecided between two sources is decided by the next complete fetch of its window
after your decision (the daily deletion check, or a `backfill` of that window); the decision itself
fetches nothing. A decision records only itself: a chain it makes possible (say `distinct` leaves
a retired watch the only candidate before a new name) is listed under `left_to_reconciler` and
decided by the next nightly run, listed for review like any other. For a chain the reconciler
suggested, `confirm` after `reject` restores what is counted but marks the chain reviewed
(`confirmed`); if the reconciler linked one of the freed sources elsewhere meanwhile, `confirm` is
refused naming that chain (reject it first). A decision waits up to a minute while a reconciler
batch run holds the single-run lock, then is refused naming it; run it again after
`healthos status` shows the run finished. If a decision or a nightly run fails after devices
changed, the rows still to settle are kept (`pending_settle`) and the next run settles them (a
`--dry-run` shows them among what it would settle).

The next morning, `healthos status` shows `last_mode: incremental` with a fresh
`last_finished_at` per data type, its `last_result` with the counts and an `open` section
(chains to review, ambiguous sources, pairs kept apart that nothing decided, pairs that look like
a rename although evidence made them two devices, refuted chains, deferred deletions), and
`identity_deferred_deletions` lists the deletions waiting for a decision; the log has
`IDENTITY_RECONCILE_COMPLETED` (and one `IDENTITY_RECONCILE_DATA_TYPE` per data type). Check
`open` after every rename you notice; anything listed there is counted twice (never hidden) until
you decide it. A data type that never had a full run gets its first full pass from the nightly
job (`skipped: no full identity-backfill of this data type yet: ran its first full pass`).
`IDENTITY_RECONCILE_LOCK_BUSY` (and `retrying in … s` on stderr) means a batch met a long sync, a
deletion check, an import or the watchdog's recovery after a restart and backed off. The run then
goes on with the other data types (`IDENTITY_RECONCILE_DATA_TYPE_DEFERRED`) and tries the busy one
again: the nightly job until the window ends (03:55 UTC, `IDENTITY_RECONCILE_UNTIL_HOUR_UTC`/
`_MINUTE`), a later run and `identity-backfill` by hand for `IDENTITY_RECONCILE_LOCK_WAIT_MINUTES`
(60; printing each retry; `0` gives up after 8 attempts).
`error: … could not get the identity lock` in that data type's `last_error` (and
`IDENTITY_RECONCILE_DATA_TYPE_FAILED` in the log) means it gave up: nothing of that batch was
written, the other data types still ran, and the next run resumes from the checkpoint.
`failed_nights` beside it in `status`, and `identity_lock_contention` in `/health` (a warning, the
status stays as it is), count the nights it has gone unsettled. More than one: run
`identity-backfill --incremental` by hand during the day (not right after a restart, while the
watchdog's recovery fetches: `WATCHDOG_RECOVERY_STARTED` in the log; the run waits for it up to
the minutes above), and check `docker compose logs app | grep -E 'RECONCILIATION_COMPLETED|IDENTITY_RECONCILE_'` for
a reconciliation that ran past 03:55 UTC. By hand, `identity-backfill` then exits 1 after printing the report. Run the incremental pass by hand with `identity-backfill --incremental [--dry-run]`.

Duration: the first pass (timelines) reads every row once, the second links in batches of 5,000
rows, each its own transaction with `statement_timeout` 120 s. On a synthetic archive of 1.4
million rows (PostgreSQL 16, developer laptop) a full run took 2 min 19 s (≈ 10,000 rows/s); an
incremental run right after under a second. Scale by your row count (`healthos status`
prints `raw_records`) and allow several times longer on a small server. It prints its progress per
batch on stderr and is idempotent: if it is interrupted (or a batch times out), run it again, or
resume the second pass where it stopped with the `--data-type ... --after ...` that the last
progress line names (the first pass always reads the whole data type, and rows with a stale start
before `--after` are still settled), then once more without them. `--batch-size` (default 5,000;
inserts are chunked for any size) and `--statement-timeout` tune it.

## Retrying failed derived days

After the computation error has been fixed, without changing `METHOD` or the source records:

```sh
python -m app.cli.derived_retry 2026-09-01 2026-09-30 Europe/Istanbul --dry-run
python -m app.cli.derived_retry 2026-09-01 2026-09-30 Europe/Istanbul
```

Both dates are inclusive and the range is 1–366 days; the time zone must be given explicitly. Only
the current method's existing `build_error` days in that range and time zone are candidates; no
cold day is created. If the source has changed, the current revision is computed under the lock.
Each day is committed in its own transaction and the lock is released between days. An interruption
rolls back only the day in progress; running again skips the successful days. Normal reads and the
scheduler do not retry failed days on their own unless the source revision changes.

The report contains the numbers of candidate, processed, successful, failed, source-changed and
skipped days; it contains no health value. Skipped days are days that are not candidates
(missing/healthy) and candidates another builder fixed while waiting for the lock. A source-changed
day is one whose source changed while it was being recomputed; the refresh queue rebuilds it and it
is not counted as failed. `--dry-run` only reads, so processed/successful/failed are zero; the
candidate count shows the work to be done. The exit code is 1 if a retry leaves a day with an error
record, 2 for invalid input, and 0 for success or a read-only run. On a database error that day's
previous error record is kept and the following days are still tried. Raw, manual and history
records do not change.

## Webhook and manual write limits

- The POST bodies of creating a manual measurement or workout, of flagging or unflagging a
  measurement and of the job starts are limited by `MANUAL_WRITE_MAX_BODY_BYTES` (default 4096 bytes, 1024–65536).
  Even when the header is absent or understated, the real bytes are counted before JSON is parsed;
  exceeding the limit returns `413`, a malformed or multiple `Content-Length` returns `400`.
- The webhook keeps its existing `GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES` limit and the signed
  original bytes; the body of an unauthorized webhook is not read.
- There are three separate per-application budgets with a fixed 60-second window: manual writes
  and job starts (`MANUAL_WRITE_REQUESTS_PER_MINUTE=60`) and two webhook budgets, each with a capacity of
  `WEBHOOK_REQUESTS_PER_MINUTE=120` (each setting 1–10000). Requests whose `Authorization` value
  matches in a constant-time comparison (challenges included) spend the authorized budget; those
  that do not match and those reaching an unconfigured receiver spend the unauthorized budget. The
  authorized budget is spent after this comparison, before the body is read and the signature
  verified; an unauthorized flood cannot push Google's deliveries down to `429`. Client address,
  UUID and proxy headers do not change the budget. Job starts (`POST /sync/all`,
  `POST /sync/{data_type}`, `POST /backfill/start`) spend the manual-write budget before their
  body is read. GET, chat, health checks and static files do not consume these budgets.
- A full budget returns `429` and a positive `Retry-After` in seconds without reading the body.
  A rate rejection writes no persistent diagnostics and does not advance the last verified
  delivery. The counters live only in process memory and reset on restart; multiple workers are
  independent of each other. The rejection counts appear in `/api/webhooks/status` under
  `process_local.rate_limited`. Short bursts on either side of the window boundary are possible.

## Mark a manual entry as a test entry

Marking an entry: on the **Ölçümler** page, "Test olarak işaretle" on its row ("İşareti kaldır" clears
it), or on the host (loopback, with the UI token):

```sh
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - -sS -X POST -H 'X-HealthOS-Request: 1' \
  http://127.0.0.1:8000/api/dashboard/measurements/<id>/flag     # .../unflag clears it
```

`GET /api/dashboard/measurements` lists the ids with their `flags`. A flagged entry stays listed with a "Test kaydı" badge; the profile card, the 3D body and Umay's daily data use the entry before it.

## Rejected Google records

Rejected records are the ones Google returned with a value jsonb cannot hold; why they exist and how the migration behaves are in the [migration notes](MIGRATIONS.md#google-records-jsonb-cannot-hold--20261102_0023).

`/health` shows `degraded` and `rejected_google_records` while an unacknowledged rejected record
exists; `rejected_records` is the number of distinct unacknowledged records. Maintenance:

```sh
docker compose exec app healthos rejections                 # last 20 records, metadata only (JSON)
docker compose exec -T app healthos rejections --element 7 > kayit.json   # record number 7 itself
docker compose exec -T app healthos export-response <response_id> > page.json   # the response it came from, byte for byte
docker compose exec app healthos rejections --ack 7         # looked at up to and including 7
```

An acknowledgement is appended to the `raw_rejection_acks` table (also append-only), cannot be
undone and only moves forward: a lower number writes nothing. After an acknowledgement, if the same
record (same data type and `element_sha256`) is rejected again, health stays green; when a new record
is rejected it becomes `degraded` again. An acknowledgement neither removes records from the archive
nor adds them to it: a rejected record does not enter `raw_health_data`, and the dashboard and Umay
do not see it. To recover a record, get its source with `export-response`; how the value is to be
represented (e.g. a non-jsonb column) is a separate maintainer decision.
