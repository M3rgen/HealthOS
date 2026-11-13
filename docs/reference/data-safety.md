# HealthOS reference — Data safety

Part of the [technical reference](../REFERENCE.md).

## What is preserved where

Nothing received from Google, and nothing entered by hand, is deleted or overwritten. Storage cost
is not a concern; every guard below is a database trigger, so it holds for every code path, the
CLI and hand-written SQL alike (`healthos verify` checks that the guards are in place).

| What | Where | Guarantee |
|---|---|---|
| Exact HTTP response bodies from the Google Health API | `raw_api_responses` | append-only (no `UPDATE`/`DELETE`/`TRUNCATE`); SHA-256 checked by `verify` |
| Current payload of every record | `raw_health_data` | no `DELETE`/`TRUNCATE`/identity change; a changed payload (hash or text) is kept as a version first |
| Every earlier payload | `raw_health_data_versions` | append-only (renumbering only during an archive import) |
| Source-deletion marks set and cleared, identity merges | `raw_health_data_events` | append-only, written by trigger (merges by `identity-backfill`) |
| Record identities (v2 key → row) | `raw_health_data_identity` | append-only (no `UPDATE`/`DELETE`/`TRUNCATE`) |
| Duplicate row → row holding its record | `raw_health_data_supersessions` | append-only; the superseded row itself is unchanged |
| Source fingerprint → the same device after a rename | `raw_source_chains` | append-only (decisions with their evidence) |
| First/last sighting per source fingerprint | `raw_source_sightings` | widen-only (trigger), written by every page and by `identity-backfill` |
| Refuted source chains | `raw_source_chain_events` | append-only (trigger) |
| Evidence of two devices (a key returned with two values in one response, and when its fetch began) | `raw_source_pairs` | append-only (trigger) |
| The owner's decisions about sources | `raw_source_decisions` | append-only (trigger); the latest per question wins |
| Each chained source's device over time (what readers count supersessions by) | `raw_source_devices` | append-only (trigger); the latest per source is current |
| Per pair of sources with evidence of two devices and per key both hold: the same reading or not | `raw_source_agreements` | append-only (trigger); the latest per key is current, summed by `verify` |
| Reconciler checkpoints and counts, deferred deletions, fetch round starts | `identity_reconcile_state`, `identity_deferred_deletions`, `fetch_rounds` | bookkeeping (no row deleted) |
| Signed webhook bodies | `webhook_events.raw_body` | the exact bytes Google posted; `payload_hash` is their SHA-256, checked by `verify` |
| Webhook batches and elements | `webhook_events`, `webhook_notifications`, `webhook_batch_items` | no `DELETE`/`TRUNCATE`; the received payload (`payload_hash`, `raw_json`, a set `raw_body`) is frozen, processing state is not; batch items append-only. `raw_json` is NULL when jsonb cannot hold the signed body (U+0000, an unpaired surrogate, a non-finite number): the bytes are in `raw_body` and such an element is rejected |
| Google records jsonb cannot hold, set apart instead of stored ([below](#records-jsonb-cannot-hold)) | `raw_health_data_rejections` | append-only (trigger); one row per record of one stored exchange, linked to its `raw_api_responses` row, with the parsed record (`element_json`, ASCII escapes) |
| The maintainer's acknowledgement of rejected records; rejected records per retrieval page | `raw_rejection_acks`, `retrieval_event_rejections` | append-only (trigger) |
| Manual measurements and workouts | `manual_measurements`, `manual_workouts` | append-only: the UI only adds entries; a correction is a new entry (for the same measurement time the newest entry wins) |
| Exact HTTP response bodies from the Garmin Connect Personal API ([below](garmin.md#garmin-connect-personal-api)) | `garmin_api_responses` | append-only (trigger); SHA-256 of the received bytes (a token exchange keeps a placeholder body) |
| Every distinct payload of a Garmin record (per kind and calendar day or activity) | `garmin_raw_records` | append-only (trigger); a changed payload is a new row, the earlier row stays; readers take the newest |
| Garmin rows of the normalized tables (`provider = 'GARMIN'`, key `garmin:<kind>:<key>`) | `steps`, `heart_rate`, `sleep_sessions`, `exercise_sessions`, … | rebuilt from `garmin_raw_records` by comparison (unchanged rows untouched, changed rows updated in place); the raw archive stays the truth |
| Health Auto Export request bodies ([Apple Health](apple.md#apple-health-github-version)) | `apple_payloads` | append-only (trigger); one row per distinct body (SHA-256), never `Authorization` |
| Every distinct version of an Apple Health record (export.xml or Health Auto Export) | `apple_raw_records` | append-only (trigger); a changed payload of the same record is a new row; readers take the newest |
| Apple rows of the normalized tables (`provider = 'APPLE'`, key `apple:<channel>:<kind>:<key>`) | `steps`, `heart_rate`, `sleep_sessions`, … | rebuilt from `apple_raw_records` by comparison; the raw archive stays the truth |
| Test-entry flags on manual measurements (`mark` / `unmark`) | `manual_measurement_flag_events` | append-only (trigger); the latest per measurement is current, the measurement is unchanged |

### Verbatim Google responses

`raw_health_data.raw_json` is the *parsed* record (jsonb normalizes spacing, key order and some
number spellings). What Google actually sent is kept separately: every HTTP response the client
receives from the Google Health API, one row per exchange, in **`raw_api_responses`**:

- `body`: the body bytes exactly as they came off the wire. The client asks for
  `Accept-Encoding: identity`; should a response arrive compressed anyway, the compressed bytes
  are stored, its `content-encoding` header is kept (and shown as `content_encoding` by
  `healthos responses` / `export-response`), and only the parser sees the decoded JSON.
  PostgreSQL's TOAST compresses the column losslessly on disk. `body_sha256` and `body_size` are
  over exactly the stored bytes; `complete = false` and `error` (e.g. `ReadTimeout`) when the
  connection broke mid-body: what arrived is still stored. `export-response` writes the stored
  bytes unchanged (pipe a `gzip` one through `gzip -dc` to read it).
- The request: `method`, `path`, `query` (the query string as sent, including `pageToken`, a
  pagination cursor) and `request_body` (the JSON of rollUp requests). Request headers are never
  stored, so neither is the access token; a credential-like query parameter would be dropped.
- `status_code` and the response headers `content-type`, `content-encoding`, `content-length`,
  `date`, `retry-after`; `attempt` (0 for the first exchange, retries and token refreshes count
  up), `fetched_at`.
- Provenance: `source` (`sync`, `webhook`, `reconciliation`, `backfill`, `gap-repair`, `probe`,
  `identity`, `devices`), `context_id` (`sync:<uuid>` per run, `webhook:<element hash>` (the
  `webhook_notifications` row), `reconciliation:<uuid>`, `backfill:<job id>`, `probe:<uuid>`) and
  `data_type`.

Error responses (4xx/5xx, including the 503s that are retried) and the probe's, identity and
paired-device responses are stored too. A data page is stored **in the same transaction as its
records**: a crash keeps both or neither, and each new or changed payload in `raw_health_data`
and `raw_health_data_versions` carries `response_id`, the page it was first read from
(`raw_health_data_history` shows it). If storing the records fails, the responses are still kept
in a transaction of their own; also when the task is cancelled (a restart or deploy between
fetching and storing, or during a retry's back-off): the responses already received are then
stored, shielded and for at most 10 seconds, before the cancellation goes on. Not stored: requests that never got a response (connection
refused, timeout before headers), OAuth token exchanges with Google's OAuth server (they carry
credentials), and webhook calls that fail authentication or the signature check (untrusted).

Measured cost: about 4–9 ms more per 1,000-record page (≈ 6–15 %) for inserting the page body in
the same transaction; on disk, the bodies roughly match the raw records themselves, plus every
re-fetch of an overlap window or reconciliation adds its page again (check with
`SELECT pg_size_pretty(pg_total_relation_size('raw_api_responses'))`).

```sh
healthos responses --data-type heart-rate --since 2026-10-20 --limit 5   # metadata, newest first
healthos responses --context webhook:<element hash>
healthos export-response 12345 --output page.json   # the exact bytes; metadata on stderr
sha256sum page.json                                      # equals body_sha256
```

`export-response` refuses (exit 1) when the stored bytes no longer match their SHA-256.

### Records jsonb cannot hold

PostgreSQL `jsonb` holds neither U+0000 nor an unpaired surrogate nor a non-finite number, all of
which Google's JSON can carry (`\u0000`, `\ud800`, `NaN`, `1e400`). One such record used to fail
its page on every attempt, so its data type never advanced. `store_records` checks every
record first (the same check as the webhook ingest, `app/google_health/jsonb.py`) and sets such a
record apart, in the page's transaction, in **`raw_health_data_rejections`** (migration
`20261102_0023`): `data_type`, `source`, `context_id`, `response_id` (the page's
`raw_api_responses` row, which holds the bytes as sent; no foreign key), `ordinal` (its place
among the page's records), `reason` (`jsonb_unrepresentable`), `element_json` (the parsed record
with ASCII escapes: nothing replaced, `NaN`/`Infinity` written as such) and its SHA-256
`element_sha256`. The rest of the page is stored and the next page is requested. Nothing is
sanitized and nothing already archived is touched: a stored record whose new payload is rejected
keeps its stored payload. One row per record of one stored exchange (`UNIQUE (response_id,
ordinal)`); responses are never merged, so every re-fetch adds its exchange and its rejection.
Without an exchange (a caller with parsed records only) `(data_type, source, ordinal,
element_sha256)` keeps a repeat out. A page that rolls back takes its rejections with it.

- A round that rejected a record marks no source deletions: the rejected record may be an
  archived one sent again, and the round cannot show it was returned. The next round of the window
  without a rejection marks what Google no longer sends.
- Retrieval receipts: a rejected record stays in `returned_count` and `returned_times` (Google
  returned it, so coverage does not call its day empty) and is not added to `unplaced_count`; the
  number per page (or failed outcome) is in `retrieval_event_rejections`.
- `/health` is `degraded` with reason `rejected_google_records` while a rejected record is not
  acknowledged; `rejected_records` counts distinct records (data type and `element_sha256`) not
  yet acknowledged. It never hides a worse state.

```sh
healthos rejections                 # newest first, metadata only, with the ack in force (JSON)
healthos rejections --element 7     # the parsed record of rejection 7 on stdout
healthos export-response 12345      # the exchange it came from, byte for byte
healthos rejections --ack 7         # every rejection up to 7 was looked at
```

`--ack` appends to `raw_rejection_acks` (append-only; it only moves forward, a lower id writes
nothing). The same record rejected again later stays acknowledged; a new record turns `/health`
`degraded` again. A rejected record is not in `raw_health_data`: dashboards and Umay do not see it.
How to represent such a value in the archive is a separate decision (RUNBOOK).

### Source deletion events

`source_deleted_at` on a record is its current state. Every change of it is also appended, by
trigger, to **`raw_health_data_events`**: `source-deleted` (marked, with the mark's time, also when
an archive import restores a record as deleted) and `source-returned` (with the fetch that saw it
again and `previous_deleted_at`, the mark being cleared). A record that disappears and comes back
therefore keeps its whole timeline; `healthos history` lists the events next to the
versions. `identity-merged` is appended (by `healthos identity-backfill`, or by the page
that creates a row of a decided device and links it; not by a trigger) when a duplicate row is
linked to the row that holds its record now (see
[Record identity](identity-and-versions.md#record-identity)); it changes nothing on the row.

### Write paths (audit)

Every `UPDATE`, UPSERT and `DELETE` in `app/`, `scripts/` and `migrations/` that touches a table
above, and why it is allowed:

| Write | Table | Verdict |
|---|---|---|
| `store_records` UPSERT (`app/services/archive/storage.py`) | `raw_health_data` | onto the row that holds the record's identity (never re-keyed); payload change → kept as a version by trigger first; `last_seen_at`, `source_deleted_at` → `NULL` (logged as an event), `payload_received_at`/`payload_source`/`response_id` change only with the payload; derived `source_*_time`/`source_timezone` |
| `store_records` aliases, evidence and links of its new rows (`app/services/identities/identity.py`, `source_chains.py`) | `raw_health_data_identity`, `raw_source_pairs`, `raw_health_data_supersessions`, `raw_health_data_events` (`identity-merged`) | `INSERT ... ON CONFLICT DO NOTHING` / inserts only |
| `store_records` sightings (`app/services/identities/source_chains.py`) | `raw_source_sightings` | first/last sighting widened in place (bookkeeping, no received data) |
| `identity-backfill`, the nightly `identity-reconcile` job and `identity-decide` (`app/services/identities/identity_backfill.py`, `identity_decisions.py`) | `raw_health_data_identity`, `raw_health_data_supersessions`, `raw_source_chains`, `raw_source_chain_events`, `raw_source_pairs`, `raw_source_decisions`, `raw_source_devices`, `raw_health_data_events`, `raw_source_sightings`, `identity_reconcile_state` | inserts only (sightings widened, marked complete); `raw_health_data` and its versions are only read |
| `mark_source_deletions` (`app/services/archive/source_deletions.py`) | `raw_health_data` | sets `source_deleted_at` only (logged as an event); nothing deleted |
| import-restored row takes a newer dump payload (`app/services/archive/raw_history.py`) | `raw_health_data` | only rows the import itself created; the old payload is kept as a version by trigger |
| version renumbering (`import-versions`) | `raw_health_data_versions` | `version` only, in a transaction with `healthos.renumber = '1'` |
| quarantine of rollup records (migration `0002`, applied long ago) | `raw_health_data` | flag columns only, payload untouched |
| batch/element processing (`app/sync/webhook_processing.py`) | `webhook_events`, `webhook_notifications` | operational: `status`, counters, `last_error`, `processed_at`, `failure_count`, `element_count`, filling a missing `raw_body`/`signature_verified_at` once; payload frozen by trigger |
| test-entry flag and its removal (`app/services/archive/manual_flags.py`) | `manual_measurement_flag_events` | inserts only; `manual_measurements` is only read |
| `signature_verified_at` backfill (migration `0003`) | `webhook_events` | operational, fills a NULL |
| OAuth connection, refresh-token replacement and key rotation (`app/services/providers/credentials.py`) | `oauth_credential_versions` | inserts only; migration `20261108_0026` blocks UPDATE/DELETE/TRUNCATE (PostgreSQL ENABLE ALWAYS triggers; SQLite UPDATE/DELETE guards) and refuses downgrade while versions exist. The highest ID for the subject is active; `oauth_tokens` is an untouched legacy fallback. Old versions are preserved. |
| sync state, backfill jobs, reliability state, appearance, display preferences | `sync_state`, `backfill_jobs`, `reliability_state`, `appearance_settings`, `display_preferences` | operational metadata, not received data |
| normalized UPSERTs | `heart_rate`, `steps`, … | derived, see below |
| `downgrade()` of migrations | — | a downgrade that would lose rows refuses ([details](../MIGRATIONS.md#downgrades-that-would-lose-data-are-refused)); versions, responses or events need `-x allow-version-loss=1` after a backup |

Outside the database: `scripts/restore.sh` drops and recreates only its scratch target (never
`health`), and `scripts/backup.sh` prunes old dump *files* by its retention setting (see
[Database backup and restore](#database-backup-and-restore)).

### Derived tables

Normalized tables (`heart_rate`, `steps`, `sleep_sessions`, `scalar_metrics`, …) and the
`analytics_*` views are **derived** from the current raw payloads and can be rebuilt from
`raw_health_data` at any time, so their UPSERTs overwrite in place and that is acceptable: no
received data lives only there.

A normalized row carries the key of its raw row, so a duplicate raw row has a duplicate normalized
row. Those rows are not deleted, flagged or rewritten: whether a row counts is decided in one
place, `raw_health_data_supersessions`, and every reader applies it through one definition
(`app/models/eligibility.py`: `raw_counted`, `normalized_counted` and its SQL text for the views). A
record counts when its raw row is live (not marked deleted at the source), neither its raw row nor
its normalized row is quarantined, and it is not superseded, through a link in force, by a row that
is neither marked deleted nor quarantined. A normalized row without a raw row never counts, and a
quarantine on either side hides the record from every reader. The dashboard (UI, CSV and history),
the workout list, Umay's archive tool (`daily`, `inventory`, `records`) and, on PostgreSQL, the
`analytics_*` views (since `20261024_0013`) all count the same records; the dashboard's
`valid_sources_only` flag, kept for older callers, is the default and changes nothing. A
rebuild of the normalized tables from `raw_health_data` therefore gives the same counts, and
nothing has to be kept in sync. A renamed record written after `20261021_0012` updates its existing normalized row (same
key), so no new duplicate appears.

## Database backup and restore

Daily backups are automated by [`deploy/systemd/healthos-backup.timer`](../../deploy/systemd/healthos-backup.timer)
(03:30 UTC, `Persistent=true` so a missed run happens after boot), which starts
[`deploy/systemd/healthos-backup.service`](../../deploy/systemd/healthos-backup.service). The service
runs [`scripts/backup.sh`](../../scripts/backup.sh) on the Docker host:

1. `docker compose --profile backup run --rm -T backup` streams a compressed custom-format
   `pg_dump` to stdout (the container refuses to write a dump to a terminal);
2. plaintext bytes are counted in memory (an empty `pg_dump` fails even if encryption emits
   a header); the stream is encrypted before it reaches the disk and written atomically as
   `BACKUP_HOST_DIR/health-<UTC stamp>.dump.age` (or `.dump.gpg`);
3. the result is written to `BACKUP_HOST_DIR/last-backup-status.json`, on success and on failure.

**Backups are kept forever.** The script deletes nothing unless `BACKUP_RETENTION_DAYS` is
explicitly set to a positive whole number of days; then, and only after a successful new backup,
`health-*` dumps older than that are removed. Anything else deletes nothing: unset, empty, `0`,
`never`, a negative number or any other text (that last one is reported on stderr, and the backup
still runs). The environment wins over `.env` whenever the variable is set there, even to an empty
value; `.env` is read only when it is not. A large value such as `36500` also works.

**Free space.** Before dumping, the script checks the free space of `BACKUP_HOST_DIR`. If it is
below `BACKUP_MIN_FREE_MB` (default: twice the newest dump, at least 1 GiB; 5 GiB before the first
dump), it refuses to start with exit status 1, writes no file and deletes nothing; systemd marks the
unit failed. After each successful backup it prints the remaining free space. A dump interrupted by
a full disk or an error leaves no partial file. To make room, add disk or move old backups (see the
[runbook](../RUNBOOK.md#disk-space-for-backups)); `BACKUP_MIN_FREE_MB=0` disables the guard.

**Status file.** `last-backup-status.json` holds `timestamp` (UTC), `result` (`success` or
`failure`), `message`, `file`, `size_bytes`, `free_bytes`, `required_free_bytes`, `retention_days`
and `pruned_files`. It is replaced atomically on every run, so a stale `timestamp` means the timer
is not running. A failed run also starts
[`healthos-backup-failure.service`](../../deploy/systemd/healthos-backup-failure.service) via
`OnFailure=`, which logs at priority `crit` (`journalctl -p crit -t healthos-backup`) and
broadcasts with `wall`; nothing leaves the host.

The encryption key never lives in `.env` or next to the backups:

| Key source | Tool | Notes |
|---|---|---|
| `BACKUP_AGE_RECIPIENT=age1…` (in `/etc/healthos/backup.env`) | `age` | Preferred: the host holds only the public key; keep the identity offline. |
| `/etc/healthos/backup.key` containing `AGE-SECRET-KEY-…` | `age` | Identity on the host; mode `600`. |
| `/etc/healthos/backup.key` containing a passphrase | `gpg --symmetric` (AES256) | Fallback when `age` is not installed; mode `600`. |

`BACKUP_KEY_FILE` overrides the key path. The script refuses to run if the key file is readable by
group or others. **Store a copy of `/etc/healthos/backup.key` (or the age identity) off the host,
e.g. in a password manager.** Losing it makes every backup unreadable; nothing can recover it. Installation steps are at the top of the service file. Run a backup now with
`make backup` (or `bash scripts/backup.sh`); the path of the new file is printed. It reads the file the
systemd unit loads, `/etc/healthos/backup.env` (`BACKUP_ENV_FILE` to use another), and reruns itself
with `sudo` when that file or the backup directory is root-only.

**Restore drill (non-destructive to the live database):**

```sh
BACKUP_AGE_IDENTITY=/path/to/healthos-backup.age \
  make restore FILE=backups/health-20261020T033000Z.dump.age   # or: bash scripts/restore.sh FILE [db]
```

An `.age` file needs `BACKUP_AGE_IDENTITY` or a key file holding the identity; a server with only
the public key (the recommended setup) must be given the identity for the drill. When `backups/` is
root-only the script reruns itself with `sudo`.

[`scripts/restore.sh`](../../scripts/restore.sh) decrypts the file (age: `BACKUP_AGE_IDENTITY` or the
key file; gpg: the key file), drops and recreates a scratch database (default `health_restore`,
never `health`), restores into it with `pg_restore --exit-on-error --no-privileges`, and prints per-table row
counts next to the live database's counts plus the restored Alembic revision. Restore uses
`--single-transaction --exit-on-error`: any SQL error rolls back the entire restore.
Before reporting success it compares the restored and live Alembic revisions. A mismatch, missing
revision or failed revision query exits nonzero; the scratch copy remains for inspection and the
live database is untouched. Do not use a mismatched dump for live restore: first validate it with
matching application code and migrations in an isolated environment. Drop the scratch database
afterwards.

A dump contains private health data and Fernet-encrypted OAuth refresh tokens. Google access after
a restore requires the same current `GOOGLE_TOKEN_ENCRYPTION_KEY` and current token format; a different
root or pre-upgrade format requires fresh consent through "Connect Google". `GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD`
is validated but not used to read tokens. Records and backups remain intact. Keep every old root and
the backup key in a safe place outside the backup directory.

**Destructive command requiring explicit confirmation:** replacing the live database deletes its
current objects. Prove a current backup with the drill above, require matching schema revisions,
and confirm the target before use. The Bash block stops on decryption or restore failure and keeps
the app stopped; PostgreSQL rolls back failed restores rather than leaving a partial replacement.

```sh
bash <<'RESTORE'
set -euo pipefail
docker compose stop app
age --decrypt --identity /path/to/identity 'backups/health-<stamp>.dump.age' \
  | docker compose exec -T postgres pg_restore -U health -d health --clean --if-exists \
      --no-owner --no-privileges --single-transaction --exit-on-error
docker compose run --rm --no-deps -T provision-app-role </dev/null   # reapplies the application role's grants
docker compose start app
docker compose exec -T app healthos verify </dev/null   # exit 0 after an honest restore
RESTORE
```

When `backups/` is root-only, run the `age` line with `sudo` as well. `pg_dump` and the server rewrite how triggers and views print, so `verify` compares them with what
the server itself makes of the expected definitions: a restored archive reports no tampering, and a
real change (say a trigger made `WHEN (false)`) still fails as `redefined`.

## Integrity verification

**Verification-only command:**

```sh
healthos verify
```

This first checks that every protective trigger exists, is `ENABLE ALWAYS` and still calls its
original function body (a disabled or replaced guard would let deletes through unnoticed), then
recalculates every raw payload hash and every history payload hash, checks that each record has
exactly one current row, that its version numbers run 1..n without gaps, that history rows match
their record's key, and that no version repeats its neighbour's payload, and reports sync errors.
It then recomputes the SHA-256 of every stored Google response body and of every signed webhook
body, and checks that every `response_id` points to a stored response, and that every identity
alias and supersession link matches the rows it names: same data type and key, no cycle of links
of any length (in any state), no link hiding a record whose payload carries a Google id, every
link within one v2 key, its recorded fingerprints and the rows' forms now one or joined by
recorded chain links (at any time: a later rename may have given the holder a newer form), every
`identity-merged` event still with its link, live chains that never fork or cycle, the device log
(`raw_source_devices`, what readers count supersessions by) equal to what the live chains make,
every chain decided by the reconciler on evidence that satisfies the rule (or by the owner, naming
its `raw_source_decisions` row), with sightings for both of its sources that never narrowed since,
every owner decision naming sources that have sightings, the identity tables' keys and checks
unchanged (keys, checks and foreign keys only: NOT NULL constraints, which PostgreSQL 18 lists
too, are not compared), no rule, row-level security or policy on a guarded table, no guard on a
table that is not guarded, and the `analytics_*` views counting exactly as 0013 defines them
(compared on their own columns: a column added to a normalized table later, which `SELECT *`
views do not pick up, is no finding), and no row without a Google id holding payloads of more
than one source in its history (a payload written onto another source's row; a missing
`dataSource` and a JSON `null` one are the same source), and the agreement counts per pair equal to
the latest per-key rows of `raw_source_agreements` summed. Notes, not errors:
records counted more than once within one device (rows of one key and reading that no link in
force joins yet: before the first full run, or until the next one), records with more than one
counted row of one source (an older value beside the one returned last, until the next complete
fetch of their window marks it), the partial index of
source-deleted rows missing (a migration that left it for later next to a dump or transaction;
the note prints the statement), any invalid index on `raw_health_data` (an interrupted concurrent
build, `REINDEX INDEX CONCURRENTLY` to rebuild it, or a `..._ccnew` copy left by an interrupted
rebuild, `DROP INDEX CONCURRENTLY` removes it: it holds no data of its own; both printed, nothing
done by verify), chains refuted or rejected since (no
longer used: the rows they linked count on their own), supersessions no longer in force,
superseded rows that count again because every row holding them was marked deleted, holders that
are superseded themselves, superseded rows holding an older value than their holder, and
superseded rows returned after their holder with another reading or end (hidden behind a stale
holder until a complete fetch of their window marks it).
Against a database still at 20261020_0011, or not yet at 20261024_0014 (without
`manual_measurement_flag_events`), it says so and names the upgrade instead of failing on
the missing tables. It exits 1 on any finding. For long-term preservation also monitor PostgreSQL disk health, retain multiple generations of dumps, and periodically restore a dump to a separate local PostgreSQL instance.
