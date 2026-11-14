# Migration notes

An index of the early revisions, then detailed notes from `20261020_0010` on: what changed, how long it holds which locks and how to roll it back. These notes moved here from the [runbook](RUNBOOK.md), which keeps the operations; each section keeps the heading it had there. Migrations run themselves on `docker compose up` (see [Apply migrations](RUNBOOK.md#apply-migrations)): this page is for upgrading across one of them with care, or for finding out why an old revision behaves as it does.

Commands that say `git pull --ff-only` show the update as it was done when the revision was new; for an installation the general update path in the [runbook](RUNBOOK.md#update-the-application) applies, including the [v0.2.2 path](RUNBOOK.md#upgrade-from-v022) where `git pull` cannot fast-forward.

## Downgrades that would lose data are refused

`migrations/env.py` installs [`app/db/downgrade_guard.py`](../app/db/downgrade_guard.py) in every
Alembic run. It wraps Alembic's own `drop_table` and `drop_column`, so it also covers revisions
written without a check of their own; no migration was edited. Upgrades never drop a table, so they
do not reach it. Restoring a backup remains the way to go back on a live archive
([Apply migrations](RUNBOOK.md#apply-migrations)).

- A table with rows is not dropped, and neither is a column that holds evidence nothing else can
  rebuild (`raw_body`, `is_quarantined`, `quarantine_reason`). The downgrade stops with
  `<table> holds rows; downgrade refused: it would lose data that cannot be rebuilt`.
- A refusal rolls the whole downgrade back. `20261017_0007` commits partway (its `DROP INDEX
  CONCURRENTLY`), so the revisions below it are checked before the first step runs: a refused
  `alembic downgrade base` changes nothing. Tested on PostgreSQL in
  `tests/integration/test_pg_downgrade_refusal.py`.
- Dropped even with rows, because they are rebuilt or are bookkeeping: `derived_days`,
  `reliability_state`, `garmin_pull_state`, `display_preferences`, `google_heart_hours` and `google_heart_projection_state`.
- The older per-revision consent flags still work, for their own tables only, and should follow a
  backup: `-x allow-version-loss=1` (`raw_api_responses`, `raw_health_data_events`,
  `raw_health_data_versions`), `-x allow-identity-loss=1` (the identity tables of `20261021_0012`) and
  `-x allow-flag-loss=1` (`manual_measurement_flag_events`). No flag reaches `raw_health_data`.
- Offline (`--sql`), revisions that must count rows refuse outright, and on PostgreSQL the
  guard writes its check into the script, so the script refuses when it runs.

**Limit.** The guard sees only those two Alembic operations. SQL a revision runs through `op.execute`
is invisible to it, and only the listed columns count as evidence: the
`raw_health_data.response_id`, `payload_source` and `payload_received_at` columns that the
downgrade of `20261020_0011` drops are protected only by that revision's own check, which
`-x allow-version-loss=1` lifts.

## Early revision index

| Revision | Change |
| --- | --- |
| `20260818_0001` | Initial raw-first schema and normalized tables |
| `20260818_0002` | Quarantine columns, pre-fix rollup flags and filtered analytics views |
| `20260818_0003` | Webhook body and verification fields, notifications and batch membership |
| `20260819_0004` | Persistent reliability state and raw update-time index |
| `20260930_0005` | Separate manual body measurements |
| `20261001_0006` | Separate manual workouts |
| `20261017_0007` | Raw stream freshness index |
| `20261020_0008` | The `abandoned` backfill job status |
| `20261020_0009` | Shared appearance settings |

## `20261020_0010` (raw_json json → jsonb)

This one rewrites `raw_health_data`, `webhook_events` and `webhook_notifications` (and rebuilds
their indexes) under an exclusive lock. It runs before the app serves, so the app is down until it
finishes: expect seconds to a few minutes for a few hundred thousand raw rows, during which
webhooks fail and Google retries them. The rewrite needs free disk of roughly the size of those
tables. It first reads every row both ways and aborts, converting nothing, if any payload would
read back differently as jsonb (`-0.0`, a float written as `1e+16`, a `\u0000` escape).

```sh
docker compose exec app alembic current          # expect 20261020_0009
docker compose exec postgres psql -U health -d health \
  -c "SELECT pg_size_pretty(pg_total_relation_size('raw_health_data'))"
df -h /var/lib/docker
make backup                                       # mandatory: the only exact rollback
git pull --ff-only
docker compose up -d --build
docker compose logs -f migrate                    # wait for "Running upgrade ... 20261020_0010"
docker compose exec app alembic current          # expect 20261020_0010 (or a later head)
docker compose exec app healthos verify     # payload hashes still match
api /health
```

If the log shows `raw_json -> jsonb would change ...` or `unsupported Unicode escape sequence`,
nothing was converted and the database is still on `20261020_0009`; check out the previous commit,
rebuild, and report the listed row keys. `alembic downgrade 20261020_0009` converts back to `json`
with every value kept, but the stored text is jsonb's spelling, not the original; restore the
backup if the original text is needed.

## `20261020_0011` (raw versions, verbatim responses, delete guards)

Adds `raw_health_data_versions`, `raw_api_responses` (exact Google response bodies),
`raw_health_data_events` (source-deletion timeline), three nullable columns on `raw_health_data`
(`payload_received_at`, `payload_source`, `response_id` with a `NOT VALID` foreign key), the
version, event and guard triggers (no `DELETE`/`TRUNCATE`/identity change on `raw_health_data`,
append-only history, responses and events) and the `raw_health_data_history` view (see
[What is preserved where](reference/data-safety.md#what-is-preserved-where)). It copies and rewrites no rows:
existing records become version 1 implicitly, and the foreign key is added `NOT VALID`, so the
upgrade is catalog-only: measured 0.02 s on a synthetic 500,000-row, 393 MB `raw_health_data`
(PostgreSQL 16), independent of table size; no extra disk; the exclusive lock on
`raw_health_data` lasts only that moment. The app restarts as for any update (a few seconds;
Google retries webhooks). From then on `raw_api_responses` grows by every page fetched (see the
size query in the reference).

**Do not deploy while the nightly backup runs** (the timer starts it at 03:30 UTC; it takes as long as
the dump). `pg_dump` holds a lock on every table until it finishes, and the upgrade needs an
`ACCESS EXCLUSIVE` lock on `raw_health_data` (and locks on the webhook and manual tables). The
upgrade sets `lock_timeout = 30s`: if it cannot get a lock within 30 seconds it stops with
`20261020_0011 could not lock its tables within 30s ... retry`, changes nothing (the database stays
on `20261020_0010`), and readers were blocked for at most those 30 seconds instead of until the dump
ends. The one-shot `migrate` service then exits and has `restart: "no"`, so nothing retries it and
the app does not start: run `docker compose up -d` again once the backup has finished. To wait
instead, check first:

```sh
systemctl is-active healthos-backup.service      # "inactive" (not "activating"): no backup running
systemctl list-timers healthos-backup.timer      # next run not within the next minutes
```

```sh
make backup
docker compose exec app alembic current          # expect 20261020_0010
git pull --ff-only
docker compose up -d --build
docker compose logs migrate | grep 20261020_0011 # "Running upgrade 20261020_0010 -> 20261020_0011"
docker compose exec app alembic current          # expect 20261020_0011 (head)
docker compose exec app healthos verify     # exit 0: guards in place, hashes match
api /health
# After the next scheduled sync (or `docker compose exec app healthos sync heart-rate`):
docker compose exec app healthos responses --limit 3   # pages are being kept
```

### Rolling back the application after `20261020_0011`

Do **not** downgrade the schema. `alembic downgrade 20261020_0010` refuses as soon as
`raw_api_responses` (or the history or event tables) holds a row, which is minutes after the first
sync, by design: downgrading would drop exactly the data this revision keeps. (`-x
allow-version-loss=1` would force it; never use it on the live archive. An offline `alembic
downgrade --sql` script contains the same check as a `DO` block.) The previous image cannot simply
be started either: its container command is `alembic upgrade head && uvicorn ...`, and its
`alembic upgrade head` fails with `Can't locate revision identified by '20261020_0011'`, so the app
never starts.

Roll back by running the previous image **with only the uvicorn command** (no Alembic), against the
0011 schema as it is:

```sh
cat > /tmp/healthos-no-migrate.yml <<'YAML'
services:
  app:
    command: ["sh", "-c", "exec uvicorn app.main:app --host $${APP_HOST:-127.0.0.1} --port $${APP_PORT:-8000} --no-access-log --no-proxy-headers"]
YAML
git checkout <previous commit>                   # the one running before the update
docker compose -f docker-compose.yml -f /tmp/healthos-no-migrate.yml up -d --build app   # plus your usual -f overrides
docker compose exec app alembic current          # still 20261020_0011: nothing was dropped
api /health
```

The previous code works against the 0011 schema: the new columns are nullable, nothing it does is
refused by the guards (it never deletes or truncates raw, webhook or manual rows), and the version
trigger still keeps every replaced payload. Verified on 2026-10-20 (PostgreSQL 16) with the release
before 0011 (commit `0b08a27`) on a database migrated to `20261020_0011`: its `alembic upgrade
head` fails as described, `uvicorn` alone starts and serves `/health/live`, its PostgreSQL
integration tests for storage, sync, dashboard and freshness pass (only its migration tests fail,
because they expect `20261020_0010` as head), and its `healthos verify` passes. While it runs,
Google responses are not stored in `raw_api_responses` and new payloads carry no `payload_source`
or `response_id`, so roll forward: check out the fixed release and run a plain `docker compose
up -d --build` (without the override; the 0011 migration is already applied, so Alembic has nothing
to do). Keep using the override file for every restart until then; a plain `up` of the old code
fails at Alembic.

## `20261021_0012` (record identity, source chains, superseded duplicates)

Adds `raw_health_data_identity` (fingerprint-specific v2 key → raw row), `raw_health_data_supersessions`
(duplicate row → the row holding its record now), `raw_source_chains` (source fingerprint → the
same device after a rename or a dropped package suffix), `raw_source_chain_events` (a chain that
turned out to join two devices), `raw_source_pairs` (evidence of two devices: one response
returned a key under two sources with two values), `raw_source_decisions` (your decisions,
`identity-decide`) and `raw_source_devices` (each chained source's device, what readers count
duplicates by), all append-only with `ENABLE ALWAYS` guards; `raw_source_sightings`
(first/last sighting per source, widen-only guard); the bookkeeping tables
`identity_reconcile_state` and `fetch_rounds`; and one partial index on `raw_health_data` (its
source-deleted rows). It replaces the `analytics_*` views so they count what the dashboard counts
(no source-deleted rows, no rows superseded by a live row through a link in force). See
[Record identity](reference/identity-and-versions.md#record-identity). It copies no rows and alters no existing table.
The partial index of source-deleted rows is built first, `CONCURRENTLY` and outside any
transaction: writers are never blocked by it, however large the table. A concurrent build that
fails (a timeout, a cancel) leaves an invalid index behind, so the upgrade starts it only while no
other session of the database holds a transaction or snapshot open (however briefly: a `pg_dump`
just begun counts) or a lock on `raw_health_data` beyond reading it, and gives it no timeout (no
lock or statement timeout, whatever the role or database sets: a dump started during the build is
waited for; writers are not). Otherwise it prints
`ix_raw_health_data_source_deleted is left for later` with the statement that builds it, and goes
on without it (the `analytics_*` views are only slower); `healthos verify` notes it until you
run that statement (`CREATE INDEX CONCURRENTLY IF NOT EXISTS ...`, also with the app running, when
no dump runs). An index left invalid by an interrupted build is not touched by the upgrade; verify
names it with `REINDEX INDEX CONCURRENTLY ix_raw_health_data_source_deleted`, and names any other
invalid index on `raw_health_data` (a `..._ccnew` copy of an interrupted rebuild: `DROP INDEX
CONCURRENTLY` it, it holds no data) - run those yourself when nothing else runs.
The foreign keys need a SHARE ROW EXCLUSIVE lock on `raw_health_data`, which a running `pg_dump`
does **not** block (tested), but a writing transaction does, and replacing the `analytics_*` views
needs each view for a moment (a long reader of a view holds it). The upgrade creates its tables
first, then in one attempt replaces the views (waiting at most 100 ms for a reader of each) and
takes the lock with `NOWAIT`; a failed attempt releases everything and is retried every few hundred
milliseconds for up to `lock_timeout` (30 s, `-x lock-timeout=...`), so no writer queues behind the
upgrade; once both are held, the foreign keys are added and it commits (writers wait
milliseconds). When no attempt succeeds it fails with `20261021_0012 could not lock its tables
within 30s`, changing nothing: stop a CLI `backfill`/`import-versions` first.

**The model.** The live write path never decides that two `dataSource` forms are one renamed
device, and it writes a record only onto a row of **its own** fingerprint (a changed payload is a
version): no row ever holds another source's payload. A record of a fingerprint joined to another
by a decided chain gets a row of its own that is linked at once to the device's row showing the
same reading (counted once); any other fingerprint gets rows of its own (`synthetic:v2s:…`). So a
rename nobody decided yet is **counted twice** (both rows visible) until the reconciler links the
rows, and a genuinely distinct device is never hidden. No returned Google record is ever
hidden: a superseded row counts again when the row holding it is marked deleted, and a returned
value that differs from what the row hiding its own shows gets a row of its own. A complete fetch
does not mark a row as deleted while another source that may be its device renamed returns the
same reading (the mark is deferred, never guessed, and listed under `identity_deferred_deletions`
in `status`). A duplicate is hidden only while the two sources it was linked across are one device:
rejecting a wrong chain (or deciding two sources distinct) counts both sides again at once, each
device once, and the opposite decision undoes that exactly. The **reconciler** (`healthos identity-backfill`) decides
chains only when every
fingerprint's whole sighting history leaves no doubt, and links the duplicates; it runs once in
full after the deploy and then **nightly** as the scheduler job `identity-reconcile`, started when
the scheduled Google reconciliations of the night have ended (incremental: rows changed since its
last run; `IDENTITY_RECONCILE_ENABLED`; with `RECONCILIATION_ENABLED=false` at
`IDENTITY_RECONCILE_HOUR_UTC`:`IDENTITY_RECONCILE_MINUTE`, 02:30 UTC). Everything it leaves open, and every
chain it decided, is listed in its report and in `healthos status`; **you** decide those with
`healthos identity-decide` (below). Double counting after a rename therefore lasts until the
next night, or until you decide a case the rule leaves open.

Order: **backup → deploy → `identity-backfill --dry-run` → review → `identity-backfill` → verify →
review the chains and open cases, decide them (`identity-decide`) → check that the nightly job is
active**. The scheduler, webhooks and manual syncs can keep running throughout: a page being
stored and a deletion check hold a shared lock per data type, each reconciler batch the exclusive
one (it tries for it without queueing writers behind it, waits in the queue at most 1 s, and once
it holds it at most 2 s for any row another transaction holds, then backs off and retries, saying
so on stderr, so no sync waits behind it for long), and a second run refuses at once with
`identity-backfill refused: another identity-backfill is running`. A `--dry-run` takes no lock.

```sh
make backup
docker compose exec app alembic current          # expect 20261020_0011
git pull --ff-only
docker compose up -d --build
docker compose exec app alembic current          # expect 20261021_0012 (or a later head)
df -h /var/lib/docker                            # room for the aliases (below)
docker compose exec -T app healthos identity-backfill --dry-run > /tmp/identity-dry.json
```

The alias table holds about one row per raw row without a Google id, a few hundred bytes each
with its index: check that the volume has room for that beside the archive before the full run.

The dry-run report's fields, deciding what the reconciler left open, the morning-after checks and the run's duration moved to [Identity review and decisions](RUNBOOK.md#identity-review-and-decisions).

Rolling back the application: as for 0011, never downgrade the schema (`alembic downgrade
20261020_0011` refuses while the append-only tables or the sightings hold rows;
`-x allow-identity-loss=1` would drop the links, chains, decisions and sightings, never on the
live archive; a re-upgrade and backfill afterwards links the same rows again without repeating
their `identity-merged` events, while your decisions would have to be made again). The previous
image runs against the 0012 schema with the no-migrate override above; it ignores aliases, chains
and links (and does not run the nightly job), so its dashboard counts the duplicates again and new
renames duplicate again until you roll forward.

## `20261024_0013` (analytics views count what every reader counts)

Replaces the `analytics_*` views (`CREATE OR REPLACE`, same columns; no table, row or index
changes): a normalized row also needs a live, unquarantined raw row with its key, as the
dashboard, its history, Umay's tools and the workout list require (docs/reference/data-safety.md, Derived
tables). It waits at most 100 ms per attempt for a reader of a view and retries until
`lock_timeout` (30 s, `-x lock-timeout=...`); when no attempt succeeds it fails with
`20261024_0013 could not replace the analytics_* views`, changing nothing: retry once the reader
(a long analytics query) has finished.

```sh
make backup
git pull --ff-only
docker compose up -d --build
docker compose exec app alembic current          # expect 20261024_0013 (head)
docker compose exec app healthos verify     # exit 0: the views are the ones of 0013
```

`alembic downgrade 20261021_0012` restores the views of 0012 the same way; the previous image
then runs against them unchanged.

## `20261024_0014` (test-entry flags on manual measurements)

Adds `manual_measurement_flag_events`: one row per decision to mark a manual measurement as a test
entry (`mark`) or to clear that (`unmark`); the latest row per measurement is its state. The
measurement rows are never changed (`manual_measurements` stays append-only). The table refuses
`UPDATE`, `DELETE` and `TRUNCATE` like the 0011 tables, and `healthos verify` checks its
triggers. Fast and lock-free for existing tables: an empty table, its index and its triggers (no
foreign key, so `manual_measurements` is not locked).

Marking an entry, what a flagged entry does, and the API are in [Mark a manual entry as a test entry](RUNBOOK.md#mark-a-manual-entry-as-a-test-entry). `alembic downgrade 20261024_0013` refuses while the table has rows (`-x allow-flag-loss=1` would drop the decisions, never on the live archive).

## Coverage and retrieval receipts — 20261025_0015

Run `alembic upgrade head` first. The migration adds the `retrieval_receipts`, `retrieval_events`
and operational `retrieval_checkpoints` tables, their indexes and the receipt guards; it does not
touch existing raw and manual records. UPDATE/DELETE and PostgreSQL TRUNCATE are refused on the
receipt/event tables; the guards are checked by
`healthos verify`. While receipts exist, a downgrade refuses the data loss.
No receipts are backfilled for the past; old successful jobs and `fetch_rounds` are not proof of
completeness.

Incremental (including webhook and reconciliation) and backfill fetches record every attempt.
The page event is committed in the same transaction as the records and the checkpoint. The
operational receipt pointer can change and is not historical evidence; it ties the token to a
specific page event. An earlier receipt is not inferred from the same token. The returned count of a rolled-back page stays in the failure event; that event does not prove the records were persisted. After the last page, a
result event is added; the receipt is not updated. Resumption is bound to the token digest of the
saved page; an old token without a receipt does not prove completeness even if it reaches the
last page. When the token is reset, a separate first-page attempt is opened. A failure, a
cancellation or a process interruption does not turn a partial attempt into a success.
If Google returns a `nextPageToken` it already gave in the same attempt (a cyclic page),
that page's records and responses are stored like a normal page, but no page event is written and
the checkpoint and `last_successful_sync` do not advance; the fetch fails with `RepeatedPageTokenError
(repeated_page_token)` and the returned count stays in the failure event. The next fetch
resumes from the same token; records that arrive again are not duplicated. Once Google is fixed, the
chain can reach the last page and prove the day.

The period and data type are selected under **Sistem → Durum → Google kapsaması → Ayrıntıları aç**. Long content
scrolls inside the window. `GET /api/sync/coverage` uses the existing session/bearer authentication:

```text
/api/sync/coverage?start_date=2026-09-01&end_date=2026-09-07&timezone=Europe/Istanbul&data_type=steps
```

Dates are inclusive, at most 31 days; `timezone` is required. `data_type` is optional. Types are
paginated alphabetically: `limit` 1–4, `offset` 0–1000; follow `next_offset` in the response. The
server reduces a page to at most 62 type/day cells. The summary counts only that page's closed
type/days. 12 KiB per response; at most 2000 receipts, 5000 event metadata records, 8 MiB of time data from pages overlapping the period and
100000 period records per type are read. Exceeding a limit returns a structured 413 instead of a
truncated success.

`has_value` uses the existing eligibility/exclusion rules; sources stay on separate rows.
For types with `presence_basis=eligible_record` it means a non-excluded record exists; it does not
prove the accuracy of the scalar or of the measurement frequency. `retrieval` is independent of it:
`completed_with_records`, `completed_empty`, `incomplete`, `not_attempted`, `unknown`. Returned records include
excluded records; it is not a count of inserted or updated rows. `returned_records` counts the
day's proven return observations (re-fetches included); it is null when the day cannot be determined.
A window's total is not distributed across days. History without receipts is not called "never
fetched": `not_attempted` is only a closed day that no attempt touched and that begins after the
first receipt (`receipts_since`, allowing for the civil UTC-offset margin); since then every Google
read leaves a receipt. An older day without receipts stays `unknown`.

Daily filters are evaluated by civil date, sleep by its end time, and other sessions by the
UTC-offset bounds of a civil start. Physical days can be 23 or 25 hours long in the selected time
zone; second precision and rollup minute alignment are preserved. Today does not count as a closed
day; the evidence horizon is the start of the first page attempt. A query that crosses midnight or
continues into the next day does not show as completed a day that was still in the future when its
first page began. Catalogs without a time
filter have no daily value or retrieval evidence (`no_time_filter`). Umay's coverage
card (schema 2, 12 KiB) uses the same receipt reader: for each metric it counts closed days by
retrieval state and separates days without a value under `without_value` into `empty_response`,
`returned_without_value`, `not_retrieved`, `incomplete` and `unknown`; when a single metric is
requested it lists consecutive gap periods (`gaps`, at most 31). A period exceeding the receipt
budget is read in smaller parts; if a single day still exceeds it, the day becomes `unknown` with `retrieval_evidence_limit`.

## Reconciliation difference history — 20261025_0016

The migration only adds the `reconciliation_runs` and `reconciliation_results` tables.
The daily/weekly job, `healthos reconcile` and the watchdog use the same reporting path.
No estimated difference report is produced from history without receipts or reports. On a
database that holds reports, a downgrade is refused; the raw archive, the versions and the
immutable retrieval receipts are preserved.

`GET /api/sync/reconciliation?offset=0&limit=20` is read with the existing session/bearer
authentication; it starts no new job. `limit` 1–100, `offset` 0–10000; invalid values return 422.
The response contains `runs`, `total`, `offset`, `limit`; for an empty history `runs=[]`, `total=0`.
Runs are ordered `started_at DESC, id DESC`, and each run's `types` results `data_type ASC`.
Pages are per run; a run's type results are never split across pages.

Each run keeps the requested range, its id, start/end, status and a safe error type. Type results
have `inserted`, `updated`, `unchanged`, `source_deleted` and
`pages_committed`. These are operations committed during that run;
no raw value, token or exception message is copied into the report. The counters are in the same
transaction as the record/page/checkpoint or deletion operation. A rollback is not counted; even if
a later page or window fails, earlier commits stay in the report. The same content arriving again
is not counted as new or changed. A token resumption from another, earlier window is not added to
this request's differences.

`completed` is written when every type's fetch has completed, `partial` when at least one requested
page or type completed before the failure, and `failed` when a failure occurred without such
progress. A cancellation is `cancelled`. Types stay `pending` before they start and `running` while
they run. If the process is interrupted before it can write a result, the report stays `running`
with a null end; reading does not turn that into success. The watchdog accepts as a successful
recovery only a finished persistent report under its own id with every type completed; the
cooldown, live sync and conflict locks are preserved.
A successful token resumption does not mean the whole range was fetched again from the first page;
the proof of completeness is still in the retrieval receipts. A source deletion is marked with
`source_deleted_at` only within the scope of the existing complete fetch; identity deferrals do
not change.

## Webhook diagnostics table — 20261025_0017

Adds `webhook_status_events` (a `reason` and a time, no value from the request) and one index, for
the causes behind [The webhook went quiet](RUNBOOK.md#the-webhook-went-quiet). A new table only: no
existing row is read or changed, and duration does not depend on the size of the archive. Downgrade
refuses while the table holds a row, and refuses `--sql` outright.

## Derived days and invalidation triggers — 20261025_0018

Adds `derived_days`, the versioned daily read model behind Umay's interpretation tools, and triggers
that mark a day as needing a rebuild when the records behind it change. Nothing from the archive
lives only in `derived_days`, so it is safe to rebuild, and no archive row, manual row or existing
guard is changed. `CREATE TRIGGER` takes a `SHARE ROW EXCLUSIVE` lock on each source table: writers wait for
it, a running `pg_dump` does not block it. The migration waits at most 5 s for a lock
(`SET LOCAL lock_timeout`), then fails with nothing changed; run `docker compose up -d` again.
Duration does not depend on the size of the archive. Downgrade drops the triggers, their functions
and `derived_days`.

SQLite gets equivalent triggers from this revision and keeps them; `20261025_0020` and the trigger
changes of `20261108_0028` are PostgreSQL only.

## Guards on the webhook diagnostics — 20261025_0019

Makes `webhook_status_events` append-only with guard triggers (UPDATE, DELETE and, on PostgreSQL,
TRUNCATE are refused). No row is changed. Downgrade removes only the guards; the rows stay, and
`20261025_0017` still refuses to drop them.

## Derived triggers fenced by data type — 20261025_0020

Narrows the PostgreSQL triggers on `raw_health_data`, `raw_health_data_supersessions` and
`raw_source_devices` to the five data types that feed derived days, so writes of other types no
longer take the advisory fence or dirty a day. No source row and no other function changes. PostgreSQL
only; a no-op on SQLite. Downgrade restores the triggers of `20261025_0018`.

## Signed webhook bodies jsonb cannot hold — 20261031_0021

PostgreSQL jsonb cannot hold U+0000 (`\u0000`), an unpaired surrogate (`\ud800`) or the
non-finite numbers (`NaN`, `Infinity`, `1e400`) that Python accepts. Such a signed body used to
fail on the write to `webhook_events.raw_json` and roll back the whole intake; the raw bytes were
never stored. This migration makes two columns nullable and adds two constraints: a row has
`raw_json` or `raw_body`; an element whose `raw_json` is NULL has been rejected. Existing rows are
neither read, changed nor deleted. Dropping NOT NULL requires an ACCESS EXCLUSIVE lock on two
tables; unlike 0012–0020, a running `pg_dump` blocks it. The migration waits at most 30 s for the
lock (changed with `-x lock-timeout=2min`); if it cannot get it, it stops with the error
`20261031_0021 could not lock ... retry`, changing nothing; run it again when the backup has
finished.

Such a body is stored byte for byte in `raw_body` (`payload_hash` remains the SHA-256 of those
bytes), and `raw_json` stays NULL. An element that cannot be held is rejected with `raw_json` NULL
and its derived columns empty; its original is in the batch's `raw_body`, in
`webhook_batch_items` order. Clean sibling elements are processed. Unstorable elements can get the
same `element_hash` for different spellings that `json.loads` reduces to one value (`1e400`, `2e999`
and `Infinity` all become inf). No data is lost: each element's bytes are in its own batch's
`raw_body` and `webhook_batch_items` order, and the element is rejected anyway. While a row with
NULL `raw_json` exists, `alembic downgrade 20261025_0020` is refused, changing nothing.

## Shared display preferences — 20261102_0022

Adds the single-row `display_preferences` table (`id = 1`, a `schema_version` and a JSON value), so
the time zone choice and body model rotation apply on every device instead of one browser. A new
table only: no lock on an existing table. Downgrade drops it even when the row exists: it is one display setting,
not received data.

## Google records jsonb cannot hold — 20261102_0023

The sync side of the webhook-body problem (0021): if a single record Google returned carried a
value jsonb cannot hold (`\u0000`, an unpaired surrogate, `NaN`/`Infinity`/`1e400`), the page was
rolled back on every attempt and that data type never advanced. Now such a record is set apart
into the `raw_health_data_rejections` table before the page is stored, in the same transaction; the
rest of the page is stored, the next page is requested and the type advances. No value is sanitized
or changed, and no row in the archive is overwritten. The bytes Google sent are, as always, in the
page's `raw_api_responses` row (one row per exchange; responses are not deduplicated); the
rejection row links to it with `response_id` and `ordinal` (the place in the page), and also keeps
a parsed copy of the record with ASCII escapes in `element_json`.

The migration only adds three new tables, their indexes and guard triggers; there is no ALTER of or
foreign key to an existing table, so it takes no lock on existing tables and can also run during the
nightly backup (`pg_dump`). All three tables are append-only (UPDATE/DELETE/TRUNCATE are refused;
`healthos verify` checks them). While one of the tables holds a row, `alembic downgrade 20261102_0022`
is refused, changing nothing.

A round with a rejection marks no source deletion (the rejected record may be a new form of an
archived record; the round cannot show it). Marking waits for the first round without
a rejection; that happens by itself once the record is fixed at Google or leaves the window. In the
retrieval receipt a rejected record stays in `returned_count` and `returned_times` (Google returned
it; coverage does not count that day as empty) and is not added to `unplaced_count`; its number is
kept separately in `retrieval_event_rejections`.

Day-to-day handling (the `/health` reason, the `healthos rejections` commands and acknowledgements) is in [Rejected Google records](RUNBOOK.md#rejected-google-records).

## Garmin raw archive — 20261107_0024

Adds `garmin_api_responses` (every HTTP exchange) and `garmin_raw_records` (one row per kind, key and
distinct payload), both append-only by guard trigger, and `garmin_pull_state` (bookkeeping per kind;
see [Garmin](reference/garmin.md#garmin-connect-personal-api)). Only new tables, their indexes and guards: no lock on
anything that exists, so a running `pg_dump` neither blocks nor fails it, and no existing row is
read. Downgrade refuses while either archive table holds a row, and refuses `--sql` outright.

## Provider of normalized rows — 20261107_0025

Adds `provider` (`NOT NULL DEFAULT 'GOOGLE'`) and `source_record_id` to each of the fifteen normalized
tables. Only `ADD COLUMN`: on PostgreSQL a constant default is a catalog change with no rewrite, so
duration does not grow with the row count (2 ms for 20,000 rows on SQLite; not timed on PostgreSQL).
No index is added and every existing row reads `GOOGLE` through the default. `ADD COLUMN` still takes a
brief `ACCESS EXCLUSIVE` lock on each table, and this revision sets no `lock_timeout`, so by
PostgreSQL's lock rules it waits for a running `pg_dump` (not measured): update outside the backup
window. Downgrade refuses while any table holds a non-Google row, whose provider would otherwise be
lost; otherwise it drops the two columns.

## Placeholder for v0.2.2 — 20261109_p001

An empty bridge with its own identity, separate from the Apple extension's `20261107_a001`.
When that real revision is absent, the migration environment translates its old stored stamp to
`20261109_p001` before planning the migration; only Alembic bookkeeping changes. When the real
revision is installed, it takes precedence and is never translated or skipped. The bridge changes
no schema or records in either direction: existing `apple_*` tables, rows and guards stay in place,
and new databases get no Apple tables. It branches from `20261107_0025` and joins the main line at
`20261109_0029`. The path is in [Upgrade from v0.2.2](RUNBOOK.md#upgrade-from-v022).

## Append-only OAuth credentials — 20261108_0026

Adds `oauth_credential_versions`: every connection, refresh-token replacement and key rotation is a
new row, the highest id for a subject is active, and guards refuse UPDATE, DELETE and (on PostgreSQL)
TRUNCATE. `oauth_tokens` is left untouched as the fallback for credentials written earlier. A new table
only. Downgrade refuses while the table holds a row, and refuses `--sql` outright.

## Recorded Google account changes — 20261108_0027

Adds `google_account_rebind_events`, append-only, one row per deliberate change of the Google account
(old and new health user id), with a unique foreign key to the credential version that made it. A new
table only; its foreign key points at the table `20261108_0026` added. Downgrade refuses while the table holds a row, and
refuses `--sql` outright.

## Deferred derived-day invalidation — 20261108_0028

Writers used to bump `derived_days` once per row inside their own transaction, so two syncs over
overlapping days could deadlock. Since this migration, triggers record each dirty day in
`derived_day_pending` (per transaction) and a deferred constraint trigger bumps the days once at
commit, in key order, so a day's `revision` changes when the writing transaction commits and is
not visible inside it. Garmin rows, which never join a Google raw row, no longer invalidate or take
the fence. A page transaction that PostgreSQL still aborts for a deadlock or serialization conflict
(`40P01`, `40001`) is retried, four attempts in total, and logged as
`DATABASE_CONFLICT_RETRY`; a remaining conflict is reported as `database_deadlock` or
`database_serialization` and is transient. Downgrading restores the previous triggers and refuses
if a pending row exists (none exists outside a writing transaction). SQLite keeps its earlier triggers.

Locks: `CREATE OR REPLACE TRIGGER` takes `SHARE ROW EXCLUSIVE` on each source table, so writers wait
and a running `pg_dump` does not block it; the migration waits at most 5 s for a lock (`lock_timeout`), then
fails with nothing changed. Duration does not depend on the size of the archive.

## Merge of the v0.2.2 line — 20261109_0029

A merge revision with parents `20261108_0028` (installs of `main`) and `20261109_p001` (the bridge for
v0.2.2 stamps). Its own identity is unchanged. It runs no SQL in either direction.
`alembic upgrade head` from either line passes through it, and
`alembic current` then prints `20261114_0036 (head)`: [Google heart projection](reference/heart-projection.md) documents its disposable packets and resumable fill; [Garmin time evidence](reference/garmin.md#time-evidence-and-recovery) documents the preceding `20261112_0035`.


## Garmin record observations — 20261111_0030

Adds append-only `garmin_record_observations`: each observation links a raw record version to
the HTTP response that returned it, with `observed_at`, a unique response/record pair and an
index on record, observation time and id. Foreign keys point to `garmin_raw_records` and
`garmin_api_responses`. UPDATE/DELETE are refused on SQLite and PostgreSQL; PostgreSQL also
refuses TRUNCATE and enables the guards in replica mode.

Only a new table, index and guards: no existing record is rewritten and no historical observation
is inferred. Downgrade refuses `--sql` outright and refuses while the table holds any row;
only an empty table is dropped.

## Garmin daily heart sample sets — 20261111_0031

Adds append-only `garmin_heart_sets` (source record id, raw record foreign key and membership
SHA-256) and `garmin_heart_members` (set foreign key and sample key, together the primary key),
plus a source/id index on the sets. These tables hold daily normalization membership separately
from the raw archive. UPDATE/DELETE are refused on SQLite and PostgreSQL; PostgreSQL also
refuses TRUNCATE and enables the guards in replica mode.

Only new tables, an index and guards: no existing row is changed and no old membership is inferred.
Downgrade refuses `--sql` outright and checks both tables before dropping either: if either holds
a row, it refuses. When both are empty, it drops members first, then sets.

## Completed Garmin daily projections — 20261111_0032

Adds `garmin_daily_projections`, append-only with UPDATE/DELETE and PostgreSQL TRUNCATE guards.
Each changed normalization records the selected raw ids per input kind and the prepared row key
(NULL when no row can be produced) for steps, distance, resting heart rate, sleep, HRV, SpO₂,
stress and body battery. The latest committed outcome controls daily reads independently of
quarantine. Inputs that were never archived produce no outcome; an empty or failed fetch does
not mean source deletion. Existing converter precedence and fallback rules are unchanged.

Values and evidence commit together, including bounded backfill chunks. Reprocessing an identical
outcome appends nothing. Existing v1/v2 archives stay readable until their window is reprocessed
by the existing bounded normalization; migration infers no evidence and rewrites no data.
Downgrade refuses while the table holds evidence and refuses `--sql` outright.

## Completed Garmin activity conversions — 20261111_0033

Adds append-only `garmin_activity_results`, with a foreign key to the selected raw activity
version and the prepared exercise key (NULL when conversion is invalid). Results commit in
the same transaction as normalized values. The latest completed result governs sports lists
and heart-rate activity windows; merely archiving a correction does not change them. Valid
activities outside the normalization window and absent records publish no result. No old
raw or normalized row is rewritten, and user quarantine stays independent. Archives without
results retain v1/v2 fallback. UPDATE/DELETE and PostgreSQL TRUNCATE are guarded, including
replica mode; downgrade refuses to discard evidence.

## Garmin normalization health — 20261112_0034

Adds `garmin_normalization_state`, separate from HTTP kind checkpoints. The singleton records
normalization attempt/success/error times and only the fixed `garmin_normalization_failed`
code; successful normalization clears the code. No existing row is rewritten and no prior
normalization outcome is inferred. Raw archives and completed chunks remain untouched.

## Apple extension merges — 20261111_a002, 20261112_a003, 20261112_a004, 20261114_a005

Published release only (the GitHub-only Apple extension). Merge revisions that run no SQL: they
join the extension's real `20261107_a001` (Apple raw tables, [Apple Health](reference/apple.md))
to the main line, `20261111_a002` at `20261109_0029`, `20261112_a003` at `20261111_0033`, `20261112_a004` at
`20261112_0034` and `20261114_a005` at `20261114_0036`, so
Alembic has one head. No parent's `down_revision` is edited; a later main-line revision gets
another merge revision like these.
