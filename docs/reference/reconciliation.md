# HealthOS reference — Reconciliation

Part of the [technical reference](../REFERENCE.md).

## Reconciliation

Defaults:

- Daily at 02:00 UTC: re-read the last 7 days.
- Sunday at 03:00 UTC: re-read the last 90 days.
- When the scheduled reconciliations have ended (the daily run, and on Sunday the weekly one
  too: whichever ends last; also when one fails or is missed): the identity reconciler
  (`identity-backfill --incremental`, job `identity-reconcile`, `IDENTITY_RECONCILE_CHAINED` in
  the log). It decides renamed sources and links the duplicate rows written since its last run,
  and lists what it leaves to the owner in `healthos status`
  ([Record identity](identity-and-versions.md#record-identity)). A reconciliation holds each data type's shared lock while
  it stores pages and checks deletions (heart-rate for 20 to 60 minutes of the 7-day run, more
  than four hours of the 90-day one), so a reconciler at a fixed 02:30 UTC met it night after
  night. A data type still busy (a long webhook sync, a watchdog recovery) is retried after the
  others until 03:55 UTC (`IDENTITY_RECONCILE_UNTIL_*`; started later, for
  `IDENTITY_RECONCILE_LOCK_WAIT_MINUTES`). With `RECONCILIATION_ENABLED=false` it runs at
  `IDENTITY_RECONCILE_HOUR_UTC`:`IDENTITY_RECONCILE_MINUTE` (02:30 UTC) instead. It may overlap
  the 03:30 UTC backup: `pg_dump` takes only `ACCESS SHARE` locks, which none of its inserts,
  updates or `ANALYZE` conflicts with, and dumps one snapshot (what the reconciler commits later
  is in the next backup), as with the reconciliations that already run then.

These use normal idempotent UPSERTs. Disable in `.env` when another scheduler instance owns the jobs.
The derived-day refresh (every five minutes) has no switch, so the scheduler keeps running and
`/health` reports `scheduler: running` even with all three jobs above disabled.
Run either depth manually with `healthos reconcile --days 7` or
`healthos reconcile --days 90`.

### Outage detection and bounded automatic recovery

The app checks persisted pipeline state at startup and every `WATCHDOG_INTERVAL_SECONDS`. Freshness
is based on the latest successful sync for frequently updated streams (`heart-rate`,
`activity-level`, `steps`, `distance`, `active-energy-burned`, `total-calories`, and
`active-minutes`). Low-frequency datasets such as sleep or oxygen saturation do not make the whole
pipeline stale merely because the source has not produced a recent record. If sync state is absent
after an existing installation, startup can use the latest raw database write as its fallback
reference; an entirely empty database does not trigger recovery automatically.

At startup, the oldest successful watermark among the frequent streams is used so one freshly
webhook-synchronized stream cannot mask an outage gap in another. A gap of at least
`STARTUP_GAP_THRESHOLD_MINUTES` permits recovery. During normal
operation, automatic recovery runs only when all frequent streams are stale. It invokes the same
read-only, raw-first reconciliation and UPSERT path as scheduled/manual reconciliation, choosing
the smallest whole-day lookback that covers the detected gap and never exceeding
`AUTOMATIC_RECOVERY_MAX_DAYS`.

Recovery is guarded by an in-process lock, a PostgreSQL advisory lock, checks for active syncs, the
reconciliation service's own lock, and a persisted cooldown. Consequently, multiple app instances
or repeated health checks cannot start duplicate reconciliations. A failed recovery is recorded for
diagnosis and waits for the cooldown; it does not loop continuously, reset watermarks, delete data,
or change the Google subscriber. Set `AUTOMATIC_RECOVERY_ENABLED=false` to retain diagnostics while
disabling automatic reconciliation.

`GET /health` is the machine-readable status surface. It returns HTTP 503 only when PostgreSQL
cannot be queried, including a read cancelled by `API_READ_STATEMENT_TIMEOUT_SECONDS`, allowing
Docker to restart a genuinely unusable app without creating restart loops for upstream staleness. Otherwise it returns HTTP 200 with `status` equal to `healthy`,
`degraded`, `stale`, or `failed`, plus database/scheduler/configuration state, webhook and sync
timestamps, pending/failed counts, stale stream names, reasons, latest raw write, last successful
reconciliation, and per-stream source freshness (with Garmin selected, also a
[`garmin` object and reasons](garmin.md#garmin-connect-personal-api)). `stale` means all frequent streams crossed the
configured age; `degraded` covers partial staleness, backlog, incomplete configuration, or a stopped
scheduler; `failed` reports unresolved failed webhook or sync state.

`identity_lock_contention` lists the data types whose last identity reconciliation gave up on a
busy lock: `data_type`, `nights` (UTC nights since its last successful run, its checkpoint `since`;
with one nightly run a night, the nights in a row it failed) and `last_attempt`. It is a warning
and never changes `status` (the data type's records are stored and counted; only the linking of
renamed sources waits); empty once a run settles it. `healthos status` shows the same count
for any error as `failed_nights` beside each data type's `last_error`.

After a host interruption the expected recovery sequence is:

1. The server boots and systemd starts `docker.service` and `cloudflared.service`.
2. Docker starts the existing `postgres` and `app` containers under `unless-stopped`.
3. Compose waits for PostgreSQL's `pg_isready` health check; the app then runs migrations and its
   own database readiness query before starting the scheduler.
4. The startup watchdog assesses the persisted gap and, when eligible, starts one bounded
   reconciliation. `cloudflared` forwards webhooks again as soon as port 8000 answers; until then
   Cloudflare returns 502 and Google retries.
5. If installed and enabled, `healthos-bridge.service` starts the optional Umay host bridge;
   otherwise use the manual launcher. See the [bridge runbook](../RUNBOOK.md#restart-the-umay-bridge).

A sync state or webhook element left `running` by a process that died is failed (and so becomes
retryable) at startup, but only once nothing has touched it for 15 minutes: a running fetch refreshes
its sync state on every page, so a fresher row may be a `healthos sync` or webhook replay still
working in another container. Rows that were fresh at startup are re-checked once the 15 minutes
have passed, and the watchdog likewise ignores `running` rows without a recent heartbeat.

If Docker itself is down, no in-container watchdog can notice. That host-level condition is visible
as a Cloudflare 502 on the webhook and must be handled by host monitoring.
