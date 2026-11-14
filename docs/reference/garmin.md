# HealthOS reference — Garmin

Part of the [technical reference](../REFERENCE.md).

## Garmin Connect Personal API

Selected with `HEALTHOS_SOURCES=garmin` (off until selected; the older `GARMIN_ENABLED=true` counts
only when `HEALTHOS_SOURCES` is absent). A step-by-step Docker path is in the
[README](../../README.md#3-connect-garmin). Garmin's Personal API is a read-only,
pull-only source for the owner's own Garmin Connect account: there is no webhook, no OAuth
consent screen and no write endpoint. HealthOS treats it as a **source independent of Google
Health** (it works without Google), in its own tables; nothing Google-shaped is touched. The exception to the "no new
external services" rule was decided by the maintainer on 7 November 2026
([CONTRIBUTING.md](../../CONTRIBUTING.md#project-rules), rule 4).

**Key and token.** The Personal API key (created on [Garmin's Personal API page](https://developer.garmin.com/gc-developer-program/personal/personal-api) with your Garmin Connect login, valid for the period you choose;
Garmin e-mails a week before it expires) lives in a file, `GARMIN_API_KEY_FILE` (default
`~/.config/garmin/connect-personal-api-key`, mode 600), and is read from that file at every
token exchange. Looser permissions emit a `GARMIN_KEY_FILE_PERMISSIVE` startup warning and the `/health` reason `garmin_key_file_permissive`; a future release will refuse to start with such a file. It is never put in `.env`, the database, a log, a response row or an error
message. The key is exchanged for a bearer token (`POST
/personalapitoken-service/personal-api/personal-api-key/token`, `Authorization: ApiKey …`; the
answer is the token as plain text, a JWT valid for 24 hours, no refresh). The token is kept in
memory only (never in the database, a log or an error message) and renewed an hour before it
expires or once when a request answers 401; a second 401 fails the pull. To renew the key,
replace the file's content; nothing restarts. In Compose, add
`COMPOSE_FILE=docker-compose.yml:docker-compose.garmin.yml` and
`GARMIN_API_KEY_HOST_FILE=/opt/healthos/secrets/garmin-personal-api-key` to `.env` next to
`HEALTHOS_SOURCES=garmin` (or `google,garmin`). That override mounts the file read-only into `app`
and `migrate` and points `GARMIN_API_KEY_FILE` at the mount, so no tracked file changes
([README](../../README.md#3-connect-garmin)). The container runs as UID 10001: install the key with
`sudo install -d -m 700 -o 10001 -g 10001 /opt/healthos/secrets` and
`sudo install -m 400 -o 10001 -g 10001 <downloaded-key> /opt/healthos/secrets/garmin-personal-api-key`.
Looser permissions emit a `GARMIN_KEY_FILE_PERMISSIVE` startup warning and the `/health` reason `garmin_key_file_permissive`; a future release will refuse to start with such a file.
A mistyped `GARMIN_API_KEY_HOST_FILE` stops `docker compose up` with `bind source path does not
exist`, naming that path (`create_host_path: false`), rather than letting Docker create it as a
root-owned directory. If the key cannot be read, `migrate` stops naming `GARMIN_API_KEY_FILE`.

**Docker setup checks.** Garmin shows a new key once, so copy it straight away. To check that the
container's user can read the installed file without creating a host account, run
`sudo setpriv --reuid=10001 --regid=10001 --clear-groups test -r /opt/healthos/secrets/garmin-personal-api-key && echo ok`
(`setpriv` is part of util-linux, installed on Debian); once Compose is configured,
`docker compose run --rm app healthos garmin-status` shows `"key_file_present": true`.
`garmin-status` makes no request to Garmin. If `migrate` exits with `unsafe or missing production
configuration: GARMIN_API_KEY_FILE`, the file is empty or not readable by UID 10001. Keep the key
file owned by UID 10001 after every renewal; on Docker hosts backed by a VM, check `garmin-status`
after changing it, because file ownership may be mapped differently there. The key mount, the
ownership check and `garmin-status` were run against the production Compose stack with a
synthetic key; the first `garmin-pull` needs a real key.

**What is pulled.** Eleven kinds, every one a GET under `https://personal.connectapi.garmin.com`
(outbound only; the UI stays on loopback and the tunnel passes only the Google webhook):

| Kind | Endpoint (per the official client) | Request unit | Key |
|---|---|---|---|
| `daily-summaries` | `/usersummary-service/v2/dailysummaries` | range, ≤ 90 days | `calendarDate` |
| `steps`, `stress` | `/usersummary-service/stats/{steps,stress}/daily/{start}/{end}` | range, ≤ 28 days (29 answers 400) | `calendarDate` |
| `body-battery` | `/wellness-service/wellness/bodyBattery/reports/daily` | range, 28 days | `date` |
| `sleep-summaries` | `/sleep-service/sleep/dailySleepSummariesByDate` | range, 31 days | `calendarDate` |
| `hrv-summaries` | `/hrv-service/hrv/daily/{start}/{end}` | range, 28 days | `calendarDate` |
| `heart-rate` | `/wellness-service/wellness/dailyHeartRate?date=` | one day | the requested day |
| `spo2` | `/wellness-service/wellness/daily/spo2acclimation/{day}` | one day | the requested day |
| `sleep` | `/sleep-service/sleep/dailySleep?date=` | one day | the requested day |
| `hrv` | `/hrv-service/hrv/{day}` | one day | the requested day |
| `activities` | `/activitylist-service/activities/search/activities` | range, 90 days, pages of 100 | `activityId` |

The 28-day limit of steps and stress was verified against the live API (400 at 29 days); the
31-day sleep and 90-day daily-summary limits are the community-reported bounds, kept
conservatively (32 and 91 days answered 200). Garmin publishes no rate limit: requests are at
least a second apart and 429/5xx are retried with exponential back-off (at most five attempts,
`Retry-After` honoured up to five minutes); a 400 is not retried. FIT/ZIP downloads of
activities and per-activity detail are phase 2.

**Response budgets.** A body is read as a stream and counted before it is decompressed, so a small
compressed answer cannot inflate into memory or into the archive: at most 64 MiB on the wire and
32 MiB decompressed per data response, 1 MiB for the token exchange, and 120 seconds for the whole
body on top of the 30-second per-read timeout (`app/core/bounded_http.py`, `app/garmin/client.py`).
Only `gzip` and `deflate` are requested. A body over budget, or in another encoding, is refused:
the exchange is still archived with `complete = false` and the reason in `error`
(`response_too_large`, `unsupported_encoding`), it is not parsed and not retried, and the run fails
for that kind (`GARMIN_RESPONSE_REFUSED` in the log). A refused compressed body is not stored at all,
because its bytes could not be searched for credentials; an uncompressed one keeps the bytes
received. The largest real answers are a few MiB of JSON.

**Archive.** Every HTTP exchange (token exchange included, retries and error statuses too) is
kept in `garmin_api_responses` (`body`, `body_sha256`, `body_size`, request `path` and `query`,
never a request header), byte for byte except for credentials: a token exchange (`kind =
'token'`, whatever its status) and a response whose body contains the bearer token in use keep
the placeholder `[redacted: garmin bearer token]` as `body`, and a kept response header carrying
one is masked; `body_sha256` and `body_size` still describe the bytes Garmin sent. Rows written
before this rule (7 November 2026) hold the token of that exchange as their body; each token
expired 24 hours after it was issued, and the rows stay (append-only), in backups too. Each
record of a response is kept in `garmin_raw_records` under its
kind and key (the calendar day, or the activity id) with the SHA-256 of its canonical JSON:
fetching a day again with the same content adds nothing (unique `(kind, key, body_sha256)`),
a changed content is a new row with its own `first_seen_at` and the earlier row stays. Readers
take the newest row per kind and key. Both tables refuse `UPDATE`, `DELETE` and `TRUNCATE`
(migration `20261107_0024`, triggers checked by `healthos verify`); a record PostgreSQL
`jsonb` cannot hold is counted (`GARMIN_RECORDS_UNSTORABLE`) and left in its response row
only. `garmin_pull_state` is bookkeeping: per kind the last attempt, success, error and the
backfill checkpoint.

**Schedule.** With Garmin selected the scheduler job `garmin-pull` fetches the last
`GARMIN_LOOKBACK_DAYS` (default 3) up to today every `GARMIN_PULL_INTERVAL_MINUTES` (default
120), at :10 past the hour and never between 02:00 and 03:59 UTC (Google reconciliation,
identity reconciler, backup). A failed kind is logged (`GARMIN_PULL_KIND_FAILED`), written to
`garmin_pull_state.last_error` and shown by `GET /api/garmin/status`; the other kinds go on and
the next run fetches the same window again. With the defaults a run makes 23 requests.

**One run per kind.** Each kind, and normalization, is held under its own PostgreSQL advisory
lock (`garmin_ownership` in `app/sync/ownership.py`), across containers too, so the scheduled job
and a CLI command never fetch one kind at the same time. A run waits for the lock: while `healthos
garmin-backfill` holds a kind, the scheduled pull, which goes through the kinds in order, stops
at that kind until the backfill has finished it. Scheduled runs do not pile up (one at a time;
missed ones are coalesced), but a long backfill can delay the pull far enough for `/health` to
report `garmin_pull_stale`. A run that loses its lock stops writing that kind and logs
`GARMIN_PULL_OWNERSHIP_LOST` or `GARMIN_BACKFILL_OWNERSHIP_LOST`.

**`/health`.** With Garmin selected, `GET /health` and `GET /api/status` carry a `garmin` object
(`last_success_at`, `last_error_at`, `last_error_code`, `lag_seconds`, `stale_after_seconds`) read from
`garmin_pull_state`. It never reads the free-text `last_error`, which may hold file paths; the code is the fixed
`garmin_pull_failed`. Reasons in `reasons`: `garmin_pull_failed` (a kind's last error is newer than its
last success), `garmin_pull_uninitialized` (not every kind has succeeded yet; expected before the first
complete pull) and `garmin_pull_stale` (the oldest kind's last success is older than the larger of
`PIPELINE_STALE_THRESHOLD_MINUTES` and two pull intervals plus two hours). Any of them turns `healthy` into
`degraded` and never hides a worse state. The `pipeline-watchdog` job runs with Garmin selected, but its
automatic recovery is Google's only: a Garmin failure is reported, and the next scheduled pull is its retry.
Tested with synthetic pulls; no failing pull against a real Garmin account has been observed.

```sh
healthos garmin-status                                  # enabled, key file present, last success/error, per kind
healthos garmin-pull [--kind steps --kind sleep]        # one incremental run
healthos garmin-backfill --since 2026-06-01 [--until 2026-10-31] [--dry-run] [--kind …]
```

`garmin-backfill` chunks the range per kind by the limits above (one request per day for the
day kinds) and keeps a checkpoint per kind (`backfill_next`); interrupted, it resumes from that
day when called again with the same `--since`/`--until`, and skips a kind that already finished
those bounds. Other bounds start over. `--dry-run` only counts the requests. Exit 1 when a kind
failed, 2 when the integration is off.

**Read-only summary.** `GET /api/garmin/daily?from&to` (at most 92 days) returns, per day with
a record, steps and distance (`steps`), resting heart rate, body battery and average stress
(`daily-summaries`), sleep seconds, stage seconds and score (`sleep`), last night's HRV
(`hrv-summaries`) and average SpO₂ (`spo2`), from the newest record of each kind, and says per
metric whether Garmin is the `primary` or a `secondary` source under the precedence policy
(`precedence`, `policy`; [Source precedence](source-precedence.md#source-precedence)).

**Normalization (phase 2).** After every pull and at the end of every backfill the window's
newest records are normalized into the *shared* normalized tables, tagged
`provider = 'GARMIN'` (`app/services/providers/garmin_normalize.py`, migration `20261107_0025`):

| Garmin record | Normalized table | Notes |
|---|---|---|
| `steps` (`totalSteps`, `totalDistance`) | `steps`, `distance` | one row per civil day, over the day's UTC bounds from the day's `heart-rate` record (`startTimestampGMT`/`endTimestampGMT`), UTC midnight to midnight without one |
| `heart-rate` (`heartRateValues`) | `heart_rate` | one row per sample |
| `daily-summaries` `heartRate.restingValue` (else `heart-rate.restingHeartRate`) | `resting_heart_rate` | at the day's UTC midnight, like Google's daily summaries |
| `sleep` (`sleepStart/EndTimestampGMT`, `sleepTimeSeconds`, stage seconds; `sleep-summaries` stands in) | `sleep_sessions` | |
| `hrv-summaries` / `hrv` (`lastNightAvg`, `status`, `lastNight5MinHigh`) | `heart_rate_variability` | `rmssd` is last night's average, the rest in `metrics` |
| `spo2` (`averageSpO2`) | `oxygen_saturation` | |
| `stress` (`values.overallStressLevel`), `daily-summaries` `bodyBattery` | `scalar_metrics` as `garmin-stress`, `garmin-body-battery` | no dedicated table; the dashboard does not show them |
| `activities` (`startTimeGMT`, `duration`, `activityType.typeKey`, `calories`, `distance`) | `exercise_sessions` | the workouts list shows them |

A Garmin row's `google_data_point_id` is its record key `garmin:<kind>:<key>[:<part>]` (no
Google id can equal it), `source_record_id` is `<kind>:<key>` and `device` the configured
`GARMIN_DEVICE_NAME`. Normalization is idempotent by comparison, not by overwrite: an existing
row with the same values is not written (so the derived-day triggers on `steps` and
`sleep_sessions` fire only for a new or changed row), a changed row is updated in place, and a
record a row cannot be built from is skipped and counted (`normalized.skipped`). Nothing
Google-shaped is written: no `raw_health_data` row, no sighting, no identity.

A window is normalized in transactions of seven calendar days, and activities in their own, at
most 500 per transaction (`app/services/providers/garmin_bounded.py`). A run interrupted between two commits leaves whole
chunks and running it again writes only what is missing or changed. The lock on `normalize`
keeps two runs apart; every normalization and status transaction checks the owner's capability.
Ownership loss or cancellation publishes no later outcome through an independent connection.

Pull, backfill and explicit normalization share a persistent normalization status, separate
from each kind's HTTP success and backfill checkpoint (`garmin_normalization_state`, migration
`20261112_0034`). An unresolved error degrades `/health` and `/api/status` with the fixed
`garmin_normalization_failed` reason; their `garmin.normalization` and `/api/garmin/status`'s
`normalization` object expose attempt/success/error times, status and the safe error code.
Neither exception text nor values enter this state or its logs. A later HTTP success cannot
clear it; only successful normalization clears the code, retaining the historical error time.
An archive without normalization status reports `unknown` without inventing a past outcome.

### Time evidence and recovery

Migration `20261112_0035` adds `garmin_time_evidence` (offset eligibility per immutable raw
heart version, including invalidity) and `garmin_day_times` (a completed day's selected raw
heart id, or NULL for unknown). Both are append-only, with UPDATE/DELETE and PostgreSQL
TRUNCATE guards, including replica mode. The migration rewrites no data or observations,
invents no past time choices, and refuses a downgrade that would discard either table's evidence.

Periodic normalization indexes missing current heart evidence in separate owned transactions
of at most seven records, then recovers at most seven pending archive days per transaction.
First/closer evidence, invalidation and A → B → A observations can reopen a completed day
outside the pull window without another download. Values and the selected time choice commit
together; unchanged runs append no evidence. Nearest-day selection, earlier-day ties and the
unknown-offset fallback are unchanged. Explicit backfills retain their requested date range.

**Why the shared tables and not a Garmin-specific set, and why only `ADD COLUMN` (measured).**
The three phase-2 options were weighed before the migration was written. (b), a provider
discriminator on the shared tables, keeps one `exercise_sessions` for both watches and lets one
resolver read both. Its risk was the migration: dropping the NOT NULL of
`google_data_point_id` needs a table rebuild on SQLite (`batch_alter_table`), which was measured
to **fail** on the `analytics_*` view that references `steps` and would have dropped the
derived-day triggers of 0018/0020; on PostgreSQL it would have taken an ACCESS EXCLUSIVE lock
plus a new partial unique index scan on fifteen tables. Two `ADD COLUMN`s per table instead
(`provider VARCHAR(16) NOT NULL DEFAULT 'GOOGLE'`, `source_record_id VARCHAR(512) NULL`) were
measured at 2 ms on SQLite with 20,000 rows, kept every trigger, index and view, and on
PostgreSQL 11+ a column with a constant default is a catalog change (no rewrite). The Garmin
record key therefore lives in the existing unique `google_data_point_id` column, and no index is
added. (a), a parallel Garmin table set, would have added tables and a second schema for the same
metrics; (c), the dashboard reading `/api/garmin/daily`, would have left Umay and the workouts
list without Garmin.

**What stays Google-only.** Every Google reader finds a normalized row through its
`raw_health_data` row by key (`app/models/eligibility.py`); a Garmin row has none and is
therefore never counted by the dashboard's Google values, coverage, the retrieval receipts, the
`/health` freshness summary, the derived days or the `analytics_*` views, and deletion marking
(`app/services/archive/source_deletions.py`) reads `raw_health_data` only, so a complete Google fetch never marks
a Garmin row (tested: `tests/test_garmin_normalize.py`). The identity layer keys sources by
`platform`, so a `GARMIN` source could never chain to the Fitbit. Garmin's own pull state and
freshness are in `GET /api/garmin/status` and the **Cihazlarım** entry.
