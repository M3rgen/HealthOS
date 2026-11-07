# HealtOS — Technical Reference

A fully self-hosted, subscription-free, read-only archive of health and fitness data exposed by the **Google Health API v4**. It preserves every source payload before normalization and maintains analytics-friendly PostgreSQL tables for heart rate, HRV, oxygen saturation, sleep, steps, exercise, and long-tail scalar metrics.

> This project uses `https://health.googleapis.com/v4`, the current Google Health API. It does not use or add functionality against the deprecated Fitbit Web API. Support for the legacy Fitbit Web API ended on September 30, 2026, and it shuts down completely on October 30, 2026; because HealtOS uses only Google Health API v4, Google OAuth, and `googlehealth.*` scopes, that shutdown does not affect it.

## Design principles

`RAW FIRST → NORMALIZE SECOND → ANALYZE LATER`

- Google Health remains the source of truth. This application only makes read requests to user data endpoints.
- Every HTTP response body Google sends is kept byte for byte in `raw_api_responses`, and every
  record parsed from it in `raw_health_data`; unknown fields are never dropped from the archive
  (see [What is preserved where](#what-is-preserved-where)).
- A Google resource name is the primary source identifier. Where a response lacks one, a
  deterministic identity is derived from type, physical time, discriminators and the stable
  data-source fields, so a renamed device or a reshaped interval is still the same record (see
  [Record identity](#record-identity)).
- Payload hashes distinguish unchanged records from source corrections. A correction never
  overwrites the old payload: every version is kept in `raw_health_data_versions` (see
  [Raw record versions](#raw-record-versions)).
- Raw and normalized UPSERTs plus their page checkpoint commit in one transaction.
- Backfill progress is stored per data type and time window, including the next page token.
- A page whose `nextPageToken` repeats one of the same attempt is stored like any page (records,
  responses, deduplication), but its token is never saved as a checkpoint and the window's
  watermark does not advance; the fetch then fails with `repeated_page_token` and resumes from
  the previous token.
- Incremental sync deliberately overlaps previous checkpoints to catch late or enriched data.
- Webhooks trigger retrieval; they are not treated as the dataset. Reconciliation repairs missed notifications.
- A webhook `DELETE` notification never deletes archive rows. The follow-up read and webhook audit record are retained.

## Architecture

### HealthOS dashboard

The application includes a Turkish dashboard at `http://localhost:8000/`.
It displays real archived data, hourly step and heart-rate charts, daily sleep,
7-day history, CSV export, synchronization status, and separate manual body
measurements. The 3D mannequin is illustrative, not a biometric reconstruction.
The dashboard includes a separate Codex conversation in an in-page panel. It uses
your locally installed Codex CLI and ChatGPT subscription through an authenticated
app-server bridge. Requested health data is sent to OpenAI for analysis. The
assistant can read daily summaries and original archived Google health records;
its data tools cannot modify the archive. Other desktop Codex chats are separate.

### Source-separated daily foundation

`health_archive daily` retains its legacy days/profile/charts and adds `derived` schema 1,
method `source-daily/6`. Use the new streams for source-aware interpretation: the entire
`dataSource` identifies a stream; no phone/band preference or cross-source sum is produced.
HRV average, RMSSD and deep-sleep RMSSD remain different definitions. Conflicting values
retain their observations and uncertainty; arrival/last-seen time never ranks them.
Daily summaries retain their civil date; steps use real UTC duration within local days,
and sleep belongs to the wake day. Intraday samples never become daily summaries.
Sleep timelines support explicit `startTime`/`endTime` entries in `stages`/`sleepStages`
lists (including `stages`/`data` wrappers), tested with synthetic payloads. Totals alone
return `timeline: null`; unknown intervals, conflicting stages, overlapping sessions,
episode boundaries and differences from supplied totals remain visible independently.

Upgrade to migration `20261031_0021` before using this version. `derived_days` is a local,
disposable read model keyed by day, timezone and method; sources remain separate inside
its validated result. `register_range` queues at most 366 requested days without scanning
the archive. The runtime refreshes at most 16 pending days every five minutes, committing
one day at a time; restarts resume the persistent revision differences. Database triggers
invalidate old/new dates, identity eligibility and future manual-profile results in the
same transaction as their source change. PostgreSQL builders fence concurrent mutations
with an advisory transaction lock; unchanged sightings do not dirty results. Migration
lock acquisition times out after five seconds; retry the upgrade after writers finish.
The tool may write only this local cache. Raw/manual/history rows remain untouched.
Requested derived values, source metadata and observations follow the existing tool path
to OpenAI, alongside the unchanged legacy and raw-record access. This adds no chat memory.
Tool reads build cold or pending days first, one short transaction per day, so ingestion
waits for one day's build at most and no day is built from a PostgreSQL REPEATABLE READ
snapshot (such a reader reports the day as stale). A failing day is stored as failed for
its revision (`state: "error"`, `derived_build_failed`; only the exception type is
logged), skipped by the refresh queue and rebuilt after its next source change.
`daily`'s `derived` card is at most 64 KiB: `detail: "full"` when everything fits,
otherwise `"summary"` (per stream value, state, uncertainties, `observation_count`;
observations, source metadata and sleep timelines are left to narrower ranges, `sleep`
and `records`), or `"omitted"` with a `suggested_range`. The legacy fields are unchanged.

### Interpretation budgets

Interpretation schema 2 actions plan the whole query before allocating output, so `limit` and
`cursor` never change these ceilings: 1,000 summaries; 40,950 `baseline`/`analyze` evidence
references, counted with the history repeated in every summary (five daily metrics × 90 days ×
(90 lookback days + the day itself), so one record a day always fits); 20,000 intraday cells and
20,000 intraday references; 20,000 session overlap references; 16 relationship regimes, 20,000
pairs and 40,000 pair references. Exceeding one returns its `*_budget_exceeded` code; narrow the
range or the metric. Only `response_budget_exceeded` (64 KiB per page) yields to a smaller `limit`.

### Umay personality

`app/personas/umay/soul.md` defines Umay's identity, warm Turkish voice, mythological
imagery, relationship to the data, and style examples. The backend reads this file for
each new site conversation and sends it as conversation-specific developer
instructions. The health analysis and tool rules remain separate base instructions
in `app/services/codex_chat.py`. Examples are style guidance, not user health data.
This does not edit global Codex settings or affect desktop conversations. There is no
scripted opening: Umay builds it from the user's first message and the local date and
time that the backend adds to every turn (from the browser's timezone).

Umay defaults to short, natural Turkish paragraphs, including when interpreting
health records. Formatting is reserved for requests or comparisons that benefit
from it. Assistant Markdown is rendered with Marked and sanitized with DOMPurify;
user messages remain literal text. The renderer retains raw streamed text so
formatting split across response chunks is not lost. Browser regression checks in
`tests/browser/umay-chat.cjs` cover streaming, formatting, unsafe HTML, and mobile
overflow (`cd frontend && npm run test:browser -- --only umay-chat`).

Edit the personality file to refine the voice. An existing conversation keeps its
initial personality; start **Yeni sohbet** to apply an edit. With the Docker image,
first run `docker compose up -d --build app` to include the changed file. Missing or
empty personality files prevent a new conversation from starting.

The personality file is not persistent memory. Closing the panel keeps the current
conversation, but a reload, reconnect, or new chat starts a fresh ephemeral thread.
Future refinements can specify preferred form of address, response length,
mythological intensity, and coaching style. Persistent memory would require a
separate, user-scoped storage design.

### Local Codex chat (Umay bridge)

The local website must remain private (the default Compose port binds to
`127.0.0.1`; the public webhook ingress exposes only the Google webhook route).

1. Install the Codex CLI so `codex` is on `PATH` and sign in with `codex login`
   using your ChatGPT account.
2. From this project directory, start the bridge with the project's dependencies:

   ```sh
   uv run --frozen --no-dev python scripts/start_codex_bridge.py
   ```

3. Apply the private `.env` values the launcher wrote and rebuild the site if needed:

   ```sh
   docker compose up -d --build app
   ```

4. Open the round Umay button on the dashboard at `http://localhost:8000/`.
   Questions and streamed responses stay on this page. Closing the panel keeps
   the current conversation; **Yeni sohbet**, reconnecting, or reloading starts
   a separate conversation. Site conversations are ephemeral, not sidebar chats.

For Linux production, install [`healthos-bridge.service`](../deploy/systemd/healthos-bridge.service)
using the [runbook](RUNBOOK.md#restart-the-umay-bridge). Its `--foreground` launcher owns the
bridge process group, forwards stops and reports unexpected exits (including exit 0) as failures.
The service preserves its token and never rewrites the project `.env`; provision the same
`CODEX_BRIDGE_TOKEN` for the service and backend. A restart does not require recreating the app.
The manual command above still detaches and reuses a healthy instance; run it again after a
host restart when no service is installed. Explicit `CODEX_BRIDGE_URL` values are preserved.
The launcher copies the current ChatGPT login into an isolated `~/.codex/healthos-bridge`
home (or under `CODEX_HOME`); service restarts retain refreshed credentials there,
with desktop plugins, shell, browser, computer-use and multi-agent tools disabled.
The bridge requires a random bearer token; only the backend receives that token.
Token and credentials are never included in frontend bundles or tool responses.
Manual logs are in `healthos-bridge/bridge.log`; service logs go to the journal.
The isolated home is mode 0700, its token, login and configuration files mode 0600.
Windows-specific steps (an isolated npm-installed CLI run through Node) are in
[legacy/WINDOWS.md](legacy/WINDOWS.md#umay-bridge-on-windows).

The bridge listens on `CODEX_BRIDGE_BIND_HOST` (process environment or `.env`), never on
`0.0.0.0`. Left empty, it uses the `docker0` address on Linux Docker Engine — the address
`host.docker.internal` resolves to in the app container via Compose's `host-gateway` mapping — and
`127.0.0.1` elsewhere (Docker Desktop forwards `host.docker.internal` to host loopback). On a Linux
host, also apply [`deploy/firewall/codex-bridge.nft`](../deploy/firewall/codex-bridge.nft) (or
`deploy/firewall/codex-bridge.ufw.sh`) so port 8766 accepts only loopback and Docker bridge traffic.
A bridge started by an older launcher listened on all interfaces: stop the PID in
`healthos-bridge/bridge.pid` and run the launcher again (see the [runbook](RUNBOOK.md#restart-the-umay-bridge)).

Usage counts against the signed-in ChatGPT account's limits. Codex must be running
and able to reach OpenAI. Missing configuration, connection failures and startup timeouts
produce safe Umay status messages. The panel retries five times with increasing waits;
**Yeniden bağlan** starts a fresh attempt budget. Sending is disabled until Umay is ready. For a
native backend outside Docker, set `CODEX_BRIDGE_URL=ws://127.0.0.1:8766` instead.
The site explicitly uses **GPT-6.1 Sol / high** for new conversations and each
turn. It does not switch to a different model when access fails; the chat reports
the error. The model and effort are visible in the chat's connection status.

The assistant first discovers archive coverage. It can query up to 366 civil days
per summary and paginate original records in windows of at most 31 days, with up
to 200 records per page. Larger periods require multiple queries. Quarantined and
source-deleted raw records are excluded; undated raw records are counted in the
inventory but excluded from date-filtered record queries. Daily summaries use the
dashboard's existing day allocation and missing-data rules. AI interpretation is
not a medical diagnosis.

The frontend is built with Vite and Three.js during the Docker image build and
served by FastAPI in the existing `app` container. There is no separate Node
runtime, frontend port, or additional production service.

Run from this project directory, with the existing `.env` file intact:

```sh
docker compose up --build -d
docker compose ps
```

Open `http://localhost:8000/`. This Compose file runs in production mode, which serves no API
documentation; see [Local API](#local-api).
After frontend changes, run `docker compose up -d --build app` to update this
permanent dashboard. `npm run build` alone only updates the host files; an
existing container keeps its bundled frontend. Port `5173` is a temporary Vite
development preview, not the dashboard restored by Docker after a restart.
The header date opens the calendar; choosing a day updates the daily cards,
seven-day table, and CSV export to that date. The main menu is centered and the
personal archive badge has been removed.
Manual measurements persist in PostgreSQL's `manual_measurements` table. The
startup migration adds this table without rewriting Google source records.
An entry made only to try the form can be marked as a test entry, and the mark
cleared again, without changing or deleting it: `POST
/api/dashboard/measurements/{id}/flag` and `.../unflag` (body optional:
`{"flag": "test", "reason": "..."}`, reason at most 200 characters; same
`X-HealthOS-Request: 1` rule as adding a measurement) each append one row to
`manual_measurement_flag_events` (`mark` or `unmark`); the latest row per
measurement (`created_at`, then `id`) is its state. A request that would not
change the state appends nothing (`200`, `"changed": false`; `201` when it
did); an unknown id is `404`. `GET /api/dashboard/measurements` adds `flags`
(the flags set now, e.g. `["test"]`) to each entry and still lists flagged
ones; the profile card, the 3D body and Umay's daily data
(`dashboard_data`) leave them out and use the entry before, or show no value.
Readers decide it in one place: `app/services/manual_flags.py`,
`excluded_measurements()` (an SQL subquery of ids) and
`excluded_measurement_ids(session) -> set[uuid.UUID]`.
The dashboard remains local-only; the public webhook proxy still exposes only
the webhook endpoint.

Daily summaries use the selected civil timezone. Sleep is assigned to the day
of waking. Asleep time is read from each session's raw payload: Google's
`summary.minutesAsleep` when present, otherwise the session period less AWAKE
and RESTLESS stages; a session without summary or stages counts its whole period
as an upper bound. Sessions ending at or before their start are skipped, so a day
of only such sessions has no value, not zero. Overlapping sessions count once;
where only stage totals exist, overlapping asleep duration is a conservative
estimate. Each day lists what its sleep value assumes in `sleep_assumptions`:
`invalid_sessions_skipped`, `from_normalized_columns` (no readable raw payload:
normalized duration less awake), `duration_from_payload` (a top-level `duration`,
not in the Google schema), `awake_unknown` and `overlap_estimated`.
HRV, oxygen saturation, and resting heart rate use daily source summaries, not
averages mixed with intraday samples. Missing data remains blank, not zero.
Step intervals crossing hours/days are distributed proportionally by duration;
hourly heart-rate charts show measured sample averages and preserve gaps.

For native development, build the UI before starting FastAPI:

```sh
cd frontend
npm ci
npm run build
cd ..
```

Node 24 is needed only for native frontend builds. Docker installs it in the
build stage. Browser preferences control timezone and model rotation; reduced
motion preferences disable rotation. Manual entry uses the browser's displayed
local timezone; values are stored with an explicit UTC offset.

```text
Fitbit Air → Google Health cloud → Google Health API v4
                                      ↓ OAuth 2.0 (read-only scopes)
                          FastAPI Personal Health Mirror
                         ↙ raw JSON      normalized tables ↘
                                  PostgreSQL
                         ↙                           ↘
                    pg_dump                    SQL/Grafana/Pandas
```

The core has two containers: `app` and `postgres`. APScheduler runs daily and weekly reconciliation inside the app, avoiding a mandatory queue or hosted scheduler. Run only one scheduler-enabled app replica; if scaling the API, set `RECONCILIATION_ENABLED=false` on all but one worker.

## Current Google Health API constraints

The implementation follows the official [v4 REST reference](https://developers.google.com/health/reference/rest), [data-type table](https://developers.google.com/health/data-types), [endpoint guide](https://developers.google.com/health/endpoints), and [webhook guide](https://developers.google.com/health/webhooks).

- Data can be queried as far back as it was recorded, but every page must be followed through `nextPageToken`.
- Most list endpoints allow up to 10,000 points per page; `sleep` and `exercise` are capped at 25.
- Endpoint type names use kebab case while filter message fields use snake case.
- Google Health OAuth scopes are Restricted. A personal project still needs the Google configuration and consent flow required by Google; broader distribution can require Google's verification/security review.
- Subscriber endpoints must be publicly reachable over HTTPS with TLS 1.2+.
- Subscriber creation performs an authenticated success handshake and an unauthenticated rejection challenge.
- Notification payloads are signed using a Tink-prefixed ECDSA P-256/SHA-256 signature. Google rotates keys and publishes the public keyset at a stable `gstatic.com` URL.
- Notifications can be retried for up to seven days, so periodic reconciliation remains necessary.
- The API currently does not provide a schema-enumeration endpoint. A versioned registry based on the official data-type table covers list, reconciled-stream, and rollup-only types; `EXTRA_DATA_TYPES` permits additions while the API evolves, while `ENABLED_DATA_TYPES` can deliberately limit consent and synchronization.
- The built-in registry includes every data type with a current read operation. Resource-schema types that are currently write-only (`menstrual-period`, `moods`, `ovulation-test`, and `symptoms`) are deliberately excluded so the mirror never requests write scopes or calls write endpoints.
- Read-only food and food-measurement-unit catalogs have no time filter. They are fetched once per backfill run, paginated, and retained raw like every other source resource.

## Free and private by default

There is no telemetry, tracking SDK, external logging service, hosted database, paid synchronization service, API proxy, or mandatory cloud backup. The mandatory software components are open-source and run on your hardware. Google Cloud/API account policies and any infrastructure or domain you independently choose are outside the project; no paid product is required by this code.

PostgreSQL has no published host port in the production Compose file. FastAPI is published only on `127.0.0.1`. The application container runs as an unprivileged UID with all Linux capabilities dropped, `no-new-privileges`, and a read-only root filesystem. PostgreSQL also drops all capabilities except the five its entrypoint needs to hand the data directory to the `postgres` user (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`) and runs with `no-new-privileges`; the on-demand backup container keeps no capabilities and a read-only root. Container logs use the `json-file` driver with rotation (`max-size: 20m`, `max-file: 5`) for PostgreSQL, the app, and Caddy. Put a self-hosted reverse proxy with HTTPS in front of it before exposing the webhook. Secrets are environment variables; tokens are never logged. OAuth refresh tokens obtained by the callback are Fernet-encrypted in PostgreSQL using a key that stays in the environment.

## Requirements

- Docker Engine with Docker Compose, or Python 3.12+ and PostgreSQL 15+
- `uv` 0.12.1 for deterministic native dependency installation
- A Google Cloud project with Google Health API access for real integration (not needed for development/tests)
- For webhooks: a domain/public HTTPS URL that routes to `/webhooks/google-health`

## Docker setup

Copy `.env.example` to the ignored `.env` and set a strong `POSTGRES_PASSWORD` and an
`APP_UI_TOKEN` (32+ characters); Compose refuses to start without either.

The base Compose file sets `APP_ENV=production` for the app service (overriding `.env`), so the
application refuses to start with missing or placeholder production values: `APP_UI_TOKEN`, Google
OAuth client, `GOOGLE_TOKEN_ENCRYPTION_KEY`, an HTTPS `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` and
`GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`. The full production setup is in [DEPLOYMENT.md](DEPLOYMENT.md).

To try the stack before you have Google credentials, add the development override, which sets
`APP_ENV=development` and also publishes PostgreSQL on `127.0.0.1:5432`:

```sh
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
```

The app runs `alembic upgrade head` before startup. Compose marks the app healthy once
`/health/live` answers; `/health` additionally queries PostgreSQL and needs the UI token
(`curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/health`).

## Native development

**Cross-platform command:**

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic upgrade head
uv run --frozen --extra dev uvicorn app.main:app --reload --host 127.0.0.1 --no-access-log
```

Useful checks:

**Verification-only command:**

```sh
uv lock --check
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev alembic current
```

For a destructive-to-test-data-only end-to-end Compose smoke test, run:

**Verification-only command — destructive only to its uniquely named temporary test volume:**

```sh
python scripts/verify_compose.py
# or: make compose-smoke
```

The script uses a uniquely named Compose project prefixed `health-mirror-smoke-`, a temporary env
file derived from `.env.example` with synthetic non-placeholder values (the app runs with
`APP_ENV=production`), a temporary password, UI bearer token, backup directory and throwaway backup key, and host
port `18080`. It builds the production image, waits for PostgreSQL and FastAPI, exercises `/health`
and the installed CLI, takes an encrypted backup with `scripts/backup.sh` (age if installed,
otherwise gpg), restores it with `scripts/restore.sh` into a second database and checks the row
counts, verifies the PostgreSQL Alembic downgrade/upgrade
lifecycle, checks the hardened container and private bindings, then removes only those isolated
resources. It does not read or modify `.env`, the normal `postgres_data` volume, or Google
credentials. Keep live-account verification evidence private; it can contain personal health data.

Deployment, routine operations, image maintenance, and Cloudflare setup are split into the
[deployment](DEPLOYMENT.md), [runbook](RUNBOOK.md), [image update](IMAGE_UPDATES.md), and
[Cloudflare Tunnel](CLOUDFLARE_TUNNEL.md) guides. Notes for Windows hosts and the earlier
Windows-to-Linux move are in [legacy/](legacy/WINDOWS.md).

## Configuration

Copy `.env.example` to `.env`; never commit `.env`.

| Variable | Purpose |
|---|---|
| `APP_UI_TOKEN` | Single-user access key for the UI and API (32–512 printable characters). Required in production; empty disables the login outside production. Sent as `Authorization: Bearer …` or exchanged for a cookie at `/login` |
| `DATABASE_URL` | Async SQLAlchemy PostgreSQL URL |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth web client; leave blank until integration |
| `GOOGLE_REDIRECT_URI` | Must exactly match the authorized redirect URI |
| `GOOGLE_REFRESH_TOKEN` | Optional environment-only refresh token alternative |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | Fernet key used to encrypt callback-obtained refresh tokens |
| `GOOGLE_API_MIN_REQUEST_INTERVAL_SECONDS` | Shared request pacing interval; defaults to `0.25` (4 requests/second) |
| `GOOGLE_CLOUD_PROJECT_NUMBER` | Numeric Google Cloud project number used by subscriber APIs; not the project ID |
| `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` | Public self-hosted HTTPS receiver URL ending in `/webhooks/google-health` |
| `GOOGLE_HEALTH_WEBHOOK_SUBSCRIBER_ID` | Stable 4–36 character subscriber ID; defaults to `personal-health-mirror` |
| `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` | Exact secret header value configured for the subscriber, e.g. `Bearer …` |
| `GOOGLE_HEALTH_WEBHOOK_PUBLIC_KEY_URL` | Google's permanent public signing keyset URL |
| `WEBHOOK_HOST` | Public DNS hostname used by the optional self-hosted Caddy TLS profile |
| `BACKUP_HOST_DIR`, `BACKUP_RETENTION_DAYS`, `BACKUP_MIN_FREE_MB` | Host directory for encrypted dumps; opt-in retention in days (default: empty, keep forever; only a positive whole number deletes, anything else keeps everything; set in the environment, even empty, it overrides `.env`); free-space guard in MiB (default: twice the newest dump, at least 1 GiB, or 5 GiB before the first dump); read by `scripts/backup.sh` |
| `BACKFILL_START_DATE`, `BACKFILL_WINDOW_DAYS` | Earliest attempted history and checkpoint window size |
| `GOOGLE_API_PAGE_SIZE`, `GOOGLE_API_MAX_RETRIES` | Page and exponential retry policy |
| `EXTRA_DATA_TYPES` | Comma-separated evolving v4 data types; defaults to interval filtering |
| `ENABLED_DATA_TYPES` | Optional comma-separated allowlist; blank enables the complete built-in registry |
| `RECONCILIATION_*` | Daily/weekly schedule and lookback lengths |
| `IDENTITY_RECONCILE_ENABLED`, `IDENTITY_RECONCILE_HOUR_UTC`, `IDENTITY_RECONCILE_MINUTE` | Nightly identity reconciler (default on; starts when the scheduled reconciliations have ended, at the hour and minute, default 02:30 UTC, only with `RECONCILIATION_ENABLED=false`; see [Reconciliation](#reconciliation)) |
| `IDENTITY_RECONCILE_UNTIL_HOUR_UTC`, `IDENTITY_RECONCILE_UNTIL_MINUTE` | End of the nightly window up to which the identity reconciler retries a data type whose lock stays busy (default 03:55 UTC) |
| `IDENTITY_RECONCILE_LOCK_WAIT_MINUTES` | How long a run started after that window end, and `identity-backfill` by hand, retries a busy data type (default 60; `0`: 8 attempts only) |
| `WATCHDOG_ENABLED`, `WATCHDOG_INTERVAL_SECONDS` | Enable local pipeline checks; defaults to every 15 minutes |
| `PIPELINE_STALE_THRESHOLD_MINUTES` | Age at which frequently updated sync streams are stale; defaults to 120 minutes |
| `STARTUP_GAP_THRESHOLD_MINUTES` | Minimum persisted sync/write gap that permits startup recovery; defaults to 120 minutes |
| `AUTOMATIC_RECOVERY_ENABLED`, `AUTOMATIC_RECOVERY_MAX_DAYS` | Enable bounded reconciliation recovery and cap its lookback; defaults to 7 days |
| `RECOVERY_COOLDOWN_MINUTES` | Minimum time between automatic recovery attempts; defaults to 360 minutes |
| `DEVICES_CONNECTED_HOURS`, `DEVICES_DELAYED_DAYS` | Cihazlarım status thresholds: a device whose newest record is at most `DEVICES_CONNECTED_HOURS` old (default 48) is `bagli`, older than `DEVICES_DELAYED_DAYS` (default 7) `kopuk`, `gecikmeli` in between |
| `GARMIN_ENABLED`, `GARMIN_API_KEY_FILE`, `GARMIN_PULL_INTERVAL_MINUTES`, `GARMIN_LOOKBACK_DAYS` | Optional read-only pull from the Garmin Connect Personal API (off by default; the key is read from a file, default `~/.config/garmin/connect-personal-api-key`; every 120 minutes outside 02:00–03:59 UTC; the last 3 days; see [Garmin Connect Personal API](#garmin-connect-personal-api)) |
| `GARMIN_DEVICE_NAME` | What Cihazlarım calls the Garmin source (default `Garmin`; the Personal API exposes no device model) |
| `APPLE_RECEIVER_ENABLED`, `APPLE_RECEIVER_TOKEN_FILE`, `APPLE_RECEIVER_ENDPOINT_URI` | Health Auto Export receiver (GitHub version; off by default; bearer token read from a file, default `~/.config/healthos/apple-receiver-token`; the optional https endpoint URI admits its host for `POST /webhooks/apple-health` only; see [Apple Health](#apple-health-github-version)) |
| `APPLE_RECEIVER_MAX_BODY_BYTES`, `APPLE_RECEIVER_REQUESTS_PER_MINUTE`, `APPLE_IMPORT_MAX_UPLOAD_BYTES` | Receiver body limit (default 32 MiB) and request budget (default 30 per minute, authorized and unauthorized apart); largest `export.zip` upload (default 8 GiB) |
| `SOURCE_PRECEDENCE_ACTIVITY`, `SOURCE_PRECEDENCE_HEALTH` | Which source is shown first when several report the same data: sport activities `GARMIN,FITBIT,HEALTH_KIT`, every other health metric `FITBIT,GARMIN,HEALTH_KIT` by default; the others stay visible as alternatives, nothing is summed; reverse a line to reverse the rule (see [Source precedence](#source-precedence)) |
| `CODEX_BRIDGE_BIND_HOST`, `CODEX_BRIDGE_URL`, `CODEX_BRIDGE_TOKEN` | Optional Umay bridge address and token; manual launcher preserves configured URLs, service reads existing credentials |

Generate an encryption key locally and put it only in `.env`.

**Cross-platform command (prints a new secret; store it immediately):**

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

## OAuth and Google Health setup

Do this only after local tests pass. Exact Google Console labels can evolve; follow the current [Google Health Cloud/OAuth setup](https://developers.google.com/health/setup) and [Google OAuth web-server documentation](https://developers.google.com/identity/protocols/oauth2/web-server).

1. Confirm Google Health API access is available to your project, then create or select the project and enable the API. As of October 3, 2026, Google states that new projects are not being onboarded; see the [official setup guide](https://developers.google.com/health/setup).
2. In **Google Auth Platform > Audience**, initially leave the personal integration in Testing and add the Google account used by Fitbit as a test user.
3. In **Google Auth Platform > Data Access**, add the read-only Google Health scopes required by the enabled data types. With the default registry these are `googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`, `googlehealth.nutrition.readonly`, `googlehealth.ecg.readonly`, `googlehealth.irn.readonly`, and `googlehealth.settings.readonly`. Set `ENABLED_DATA_TYPES` first if you want a smaller consent surface.
4. Create an OAuth 2.0 **Web application** client.
5. Add the exact authorized redirect URI, initially `http://localhost:8000/auth/google/callback`. No JavaScript origin is needed for this server-side flow.
6. Copy `.env.example` to the untracked `.env`. Put only `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a newly generated `GOOGLE_TOKEN_ENCRYPTION_KEY` there; never send these values in chat.
7. Restart the app and visit `http://localhost:8000/auth/google/start` while signed into the same Google account used by the Fitbit app. Perform the capability probe and small `steps` sync below while still in Testing.
8. Before relying on unattended synchronization, move the OAuth audience to **In production** under Google's applicable personal-use exception, or complete any verification Google requires for your configuration. Repeat the OAuth route afterward so the mirror stores a production-mode refresh token. Google documents that Testing-mode refresh tokens expire after seven days.

The app requests only the read-only scopes represented by its configured data types, with `access_type=offline`. The callback encrypts the refresh token, calls `users/me/identity`, and stores the stable Health user mapping. If Google later rotates the refresh token during an access-token refresh, the replacement is encrypted and committed before it is used on the next restart. Do not paste credentials into source files, issues, logs, or chat.

Before writing any health records, run the metadata-only capability probe. It requests at most one recent point per configured type and reports accessibility, timestamps, and top-level field names without returning or storing health values.

**Verification-only command:**

```bash
health-mirror probe --days 30
curl -H "Authorization: Bearer $APP_UI_TOKEN" 'http://127.0.0.1:8000/google-health/capabilities?days=30'
curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/google-health/devices
```

Run `health-mirror` inside the container (`docker compose exec app health-mirror …`) or in the
native environment. Every HTTP route except `/health/live`, `/login`, the OAuth callback and the
webhook needs the UI token: `Authorization: Bearer <APP_UI_TOKEN>`, or the login cookie in a
browser. The cookie names a random session issued at login and held in memory for at most 30 days;
`POST /logout` (same-origin form, the Sistem panel's **Çıkış yap**) revokes it, and an app restart
ends all sessions. See [DEPLOYMENT.md](DEPLOYMENT.md) for opening the UI as `healthos.localhost`.

Use this to confirm the account is linked, Fitbit data is visible, and the configured scopes cover the intended types. Then verify a small persisted sync before starting the full backup.

**Writes retrieved records to the configured database:**

```bash
health-mirror sync steps
health-mirror status
```

## Historical backfill

**Starts a persisted backfill through the local API:**

```bash
curl -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' \
  'http://127.0.0.1:8000/backfill/start?data_type=steps'
curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/backfill/status
```

**Starts a persisted backfill and waits:**

```bash
health-mirror backfill --data-type steps
health-mirror backfill
health-mirror backfill --reset-abandoned   # requeue abandoned windows, then run
```

The default attempts history from 2010 in 31-day windows. Empty windows are valid and become completed checkpoints. Recent windows run first. On interruption, rerunning resumes the saved window and `pageToken`; completed windows are skipped. A window that keeps failing is moved behind the others after five failures and marked `abandoned` after ten, so it cannot stall its data type; see the [runbook](RUNBOOK.md#abandoned-backfill-windows). Use a conservative start date to maximize completeness—the official API permits queries back to the earliest recorded data.

The `food` and `food-measurement-unit` resource catalogs remain discoverable but are not included in automatic mirror jobs. Google returns the global public food reference catalog through these unfiltered types; it is not personal health history. Personal `nutrition-log` records remain fully mirrored. Any reference rows already fetched remain preserved in the raw archive.

## Incremental sync

**Writes retrieved records to the configured database:**

```bash
health-mirror sync sleep
health-mirror sync
curl -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/sleep
curl -X POST -H "Authorization: Bearer $APP_UI_TOKEN" -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/all
curl -H "Authorization: Bearer $APP_UI_TOKEN" http://127.0.0.1:8000/sync/status
```

Default overlaps are 60 minutes for heart rate, 12 hours for steps/activity streams, 72 hours for sleep/temperature derivations, seven days for daily metrics, and 24 hours otherwise. UPSERT makes every replay safe; a changed payload keeps the previous one as a [version](#raw-record-versions).

### Source deletions

Google's list responses carry no tombstones; a deleted record simply stops appearing. When a window
was fetched from its first page to its last (an incremental run, a webhook re-fetch, or a
reconciliation), archived records that the same filter must have matched but that were not returned
get `source_deleted_at`. Nothing is deleted; dashboards and Umay skip marked records, and a record
that comes back is unmarked. The marked range is derived from exactly the bounds sent to Google
(UTC, whole seconds); rollup and reconcile types (aggregates) and catalogs without a time filter are
never marked.

- Session filters use civil start times (sleep: physical end time, exclusive). A webhook re-fetch
  of one sleep or exercise session therefore widens its window (sleep: one second past the
  session's end; other sessions: 12 hours before and 14 hours after, the range of UTC offsets) so
  the deleted session is inside both the query and the marked range. Outside webhooks, a
  scheduled window only marks sessions that started at least 12 hours after its start and 14 hours
  before its end; the rest is covered by the daily 7-day reconciliation.
- A round that set a record apart as unstorable ([records jsonb cannot hold](#records-jsonb-cannot-hold))
  marks nothing; the next round of the window without one does.
- A record whose time was moved upstream out of the window that is re-fetched looks deleted from
  that window and is marked until a fetch covering its new time returns it again (and unmarks it).
  For a move of more than 7 days that is the weekly 90-day reconciliation.
- "Not returned" is decided per [record identity](#record-identity), not per row. A row is not
  marked while the same fetch returned its v2 key **with the same reading** under **the same
  device** (its own fingerprint, or one joined to it by a live chain: a duplicate of the record
  under another id; every link in force joins two such rows) - for a growing interval, either
  by a row it is hidden behind (a link in force) with the same end or a later one (the period
  grew, under either form, however the two forms were linked meanwhile: its start is returned,
  so it is not gone), or with the same end by another fingerprint of its device. A growing row
  that would otherwise count beside the end returned now is marked: a longer end than the one
  returned (stale; it would hide the one Google returns), an end its device returned under
  another form while nothing hides this row, or a copy its own fingerprint holds in another row
  (written while this one was hidden behind another end). Otherwise a row whose reading the
  fetch returned under a fingerprint that is not *certainly* another device is *deferred*
  (`SOURCE_DELETIONS_DEFERRED` in the log; one row per deferred raw row in
  `identity_deferred_deletions`, kept until a later complete fetch of a window that holds it
  decides it - marks it, keeps it, or sees it returned - so a fetch of another window never
  drops what an earlier one deferred; listed per pair of sources by `health-mirror status`
  under `identity_deferred_deletions` and by every reconciler run under
  `deferred_deletions`), never guessed - also when the row's own device returned another value,
  since the chain that makes it one device may be the wrong one. *Certainly another device*: the
  owner decided the two `distinct` (a form of each, joined to it only by chains the owner linked
  or confirmed), or the two fingerprints themselves are established distinct by evidence while
  their sightings do not merely touch and the keys they share do not mostly agree - judged on
  every key both hold, all counted (below), never on part of them - and the keys
  this fetch returned under the other fingerprint did not mostly come back with the rows'
  readings (records reprocessed under a new name). A mark is permanent, so it is never made on the
  strength of evidence the owner may still overturn. Otherwise (not returned at all, or only with
  another value by its own device, or the same reading only by a source certainly another device)
  the row is marked: of two devices that recorded at one instant, one can be deleted alone. Only a fetch from page 1 marks (a resumed fetch does not,
  whatever its pages returned). Superseded rows are candidates too (a superseded row counts again
  when its holder is marked, so it must be marked itself when its record is gone), except one
  whose holder this fetch saw unchanged since they were linked with the same reading: that is the
  holder's record, and it is not read again on every complete fetch. Candidates are read, decided
  and marked in chunks of 2,000, so a long window is never held in memory; whether a chunk's rows
  are held that way is looked up for its ids (by the links' primary key), so the work grows with
  the window, never with its square. A row another
  transaction holds (a page writing it, so it is being seen) is skipped (`SKIP LOCKED`), never
  waited for, so marking and a page never wait for each other in a circle; a deferral of a
  skipped row stays open, since nothing was decided about it. Deferrals are written under a
  per-data-type transaction lock taken only when free: while another complete fetch of the data
  type is writing them (until it commits), a fetch writes none (`SOURCE_DELETIONS_DEFERRALS_BUSY`)
  rather than queue behind it; the next complete fetch of the window records them. Marking records
  nothing else (no evidence of two devices: that comes only from Google responses). A deletion
  deferred between two sources is decided by the next complete fetch of its window after the
  owner's or the reconciler's decision (the decision fetches nothing itself); a pair the owner or
  the reconciler joined into one device is no longer listed at once, and the next complete fetch
  of the data type, of any window, resolves its deferrals.

## Record identity

Records with a Google `name` are identified by it. Most data types (heart rate, steps, zones,
activity level, intervals, daily summaries, rollups) arrive without one; until migration
`20261021_0012` they were keyed by a hash of the raw `interval`/`sampleTime`/`date` object, the
zone fields, the **whole** `dataSource` and the top-level start/end (v1, `synthetic:<hash>`). Each
of these upstream changes therefore made the same record a new row: Google renaming a device
(`dataSource.device.displayName`), Apple Health dropping the `.<UUID>` suffix of its package name,
Google adding civil times and UTC offsets inside `interval`, and a sedentary period whose end grows
every 15 minutes while its start stays.

The **v2 identity** (`app/parsers/identity.py`) hashes only:

- the data type;
- the physical UTC start and end (intervals; rollups: their top-level window) or the instant
  (samples; daily records: their civil date), as parsed, never the raw time objects, so civil
  times and offsets do not count;
- for **growing intervals** the start alone. Only `sedentary-period` is known to behave like that
  (in the archives examined, no other type had two records with the same start, source and
  discriminators but different ends; sleep and exercise carry Google ids);
- the discriminators `heartRateZone`, `heartRateZoneType`, `activityLevelType`,
  `measurementLocation`, `measurementMethod`, `swimStrokeType`;
- the stable source fields `platform`, `recordingMethod`, `device.formFactor`,
  `device.manufacturer` and `application.packageName` without a trailing `.<UUID>`.

Left out (but kept in the record's *fingerprint*): `displayName` and every other `dataSource`
field, and a growing interval's end. The value never belongs to the identity; a changed value of
the same identity is a new [version](#raw-record-versions) of the same row.

`device.formFactor` and `device.manufacturer` are **optional device fields**: Google may add one
to a source's records that it always sent without (it began sending `formFactor:
"FITNESS_BAND"` for a band known until then by its name alone, for some data types without the
name, and not for every record). They stay in the key, so no stored key or row id changes, but
rows of two fingerprints are compared by their **match key** (the key without them; the key
itself for a source that has none). Two fingerprints are **kindred** when they are of one stable
class (a rename, as always), or when their `dataSource` is the same but for device fields one has
and the other lacks: an optional device field, perhaps with the `displayName`. A form with the
field added is then a new form of the source, like a rename: the deletion marking defers on it,
the reconciler chains it and links its rows, the owner can decide it. A source with another
name *and* the field, or a phone and a watch, is not kindred: never compared, never in the way of
a chain, exactly as before. Kindred is not transitive (a source without `formFactor` is kindred
to a phone and to a watch of its name), so a chain or an owner's link is only ever made between
two devices whose forms are **compatible** two by two (no optional device field with two
values), and no supersession ever joins two incompatible rows: a device is never a phone and a
watch. In data without optional device fields nothing of this changes anything; the
reconciler's `identities` and `distinct_sources` count per match key (equal to the key there).

**Renames are decided per source, by the reconciler or the owner.** Fitbit sends no serial
number, only `displayName`, and Apple Health sometimes differs only by the package's `.<UUID>`
suffix, so a renamed device and a second device recording at the same instant look alike in one
record. The question is therefore asked of the *sources*, per data type and group of kindred
fingerprints (without optional device fields: exactly one stable class, whose fingerprints are
the only ones that can share a key).

*Evidence of two devices* (`raw_source_pairs`): a key that **one Google response** returned under
two fingerprints **with two different readings**, noted with when its fetch began. One record has
one value: a renamed device (or one whose old name comes back) never returns two values for one
record, two devices that measured one instant do. Two fingerprints are **established distinct**
only by such evidence for three different keys (`PAIR_KEYS`) **from at least two fetches**, a later
one returning such a key for a record **newer than the first fetch** (both sources still produce
new records side by side). One response is never enough, however many keys it holds: while Google
switches a name it can return a batch of old records in both forms with values one off; nor are
old records returned again in both forms. With equal readings a co-returned key is no evidence at
all. The write path records it per page, at most three keys per fetch (the newest first) and only
while they can still help (a pair that cannot be established by a key gains no more); a full
`identity-backfill` derives it from rows of one key that one page stored or last saw together
(equal `first_seen_at` or `last_seen_at`: one transaction's clock). Rows merely seen minutes apart
are no evidence (a rename in the middle of a fetch looks exactly like that). The owner's
`distinct` / `not-distinct` decision overrides the evidence. Distinctness holds between
**devices**: a source distinct from one form of a device is distinct from all its forms. A pair
established distinct by evidence alone whose sightings only touch (one began within a day of the
other's end, `RENAME_OVERLAP`) looks like a rename: every run's report and `health-mirror status`
list it under `review_distinct` until the owner confirms it (`distinct`) or decides
`not-distinct` and links, and no deletion is marked on its strength.

The chain rule works on every fingerprint's sighting span in `raw_source_sightings` (first and
last time a record carrying it was returned, stamped with the **fetch time of the response**, so
a page that waited for a lock is still seen when Google returned it).
`app/services/source_chains.py` links *A* → *B* in a device's **chain** (`raw_source_chains`) only
when

1. every fingerprint of the class has a complete span (built from the whole archive by a full
   `identity-backfill`, or first seen after that);
2. *B* was first seen **more than 5 minutes** after *A* was last seen (`CHAIN_GAP`);
3. *A* is *B*'s only predecessor candidate (last seen before *B* appeared, with nothing of the
   class appearing in between) and *B* is *A*'s only successor candidate (first to appear after
   *A* ended);
4. no other fingerprint of the class was first seen in between, or was seen **alongside** either
   *A* or *B* (overlapping spans: a second device of the class makes any rename near it undecided);
5. *A* and *B* are not established distinct, neither already has a live link the other way (a
   chain never forks; no cycles: *A* → *B* → *A* is never a chain), and no link between them was
   rejected or refuted (the owner decides that pair again).

A fingerprint **established distinct from** *A* or *B* and with evidence for three keys against
the other (from any responses: a second watch worn alongside the band, before and after its
rename, whose evidence against the new name may come from one re-fetch of old records) is left
out of rules 3 and 4: it is known to be another device, so it says nothing about *A* → *B* (a
chain merges equal readings only, and no deletion is marked on that weaker evidence). One established distinct from *B* is never *B*'s
candidate. Nor is one that **retired before** *A*: established distinct from *A* and with no record
newer than seven days before *A*'s newest record and *B*'s first sighting (the sightings keep the
physical time of each fingerprint's newest record, `newest_record_at`, which a re-fetch of old
records does not move): it stopped recording while *A* went on for days, so it is neither *B*'s
earlier form beside *A* nor seen alongside *B* when Google returns its old records again (a
second watch retired before the band was renamed). A device that kept recording until the rename,
or was only left unworn for hours or a few days (a watch on its charger), still blocks it: *B*
may be that device renamed.

A three-step rename *A* → *B* → *C* chains step by step even when *B* only ever returned new
instants and never met *A*'s rows. Everything else is **ambiguous**: nothing is linked, both
sources keep their rows (visible, at worst counted twice, never hidden), and every run's report
and `health-mirror status` list the case under `ambiguous` with its reason (`a source of its class
ended less than five minutes before`, `more than one source of its class ended before it`,
`another source of its class appeared in between`, `another source of its class was seen
alongside`, `history incomplete`, …) until the owner decides it (`identity-decide`, below). A
typical device rename leaves hours between the old name's last and the new name's first sighting;
a rename between two pages of one fetch (minutes) is left to the owner.

A link joins its two fingerprints into **one device in both directions** and records the spans it
was decided on as `evidence`. The old form coming back later is no contradiction (a name can
change back): its records get rows of their own again, linked to the device's (`returned` in the report). A link is
**live** until it is **refuted** (its two fingerprints became established distinct: a `refuted`
event in `raw_source_chain_events`, recorded by the reconciler), the owner decides the two
`distinct`, or the owner's latest decision about it is `reject`; a later `not-distinct`, `link` or
`confirm` makes it live again. Only live links make devices, and only live links must form chains
(one successor and one predecessor per fingerprint, no cycle: checked when a link is recorded or
made live again, and by `verify`); a rejected or refuted link frees both its fingerprints for
another link. Whenever devices change, the device of every chained fingerprint is appended to
`raw_source_devices` (the latest row per fingerprint is current). A supersession records both
rows' fingerprints and is **in force** only while they are one, or one device there: readers
(dashboards, Umay, the `analytics_*` views, `held_elsewhere`) skip a superseded row only through
a link in force, so a wrong link is undone in what they count the moment it is rejected or
refuted, and done again by the opposite decision. `verify` notes links no longer in force and
checks the device log against the chains. Every link the reconciler decides is listed for
**review** (report, `health-mirror status`) until the owner confirms or rejects it.

**Writing a record** (`store_records`, `app/services/identity.py`). The write path never decides a
rename; it applies decided chains (a *device* is a fingerprint and every one joined to it by usable
links, either way). **A payload is only ever written onto a row of its own fingerprint**, so a row's
source never changes and no row's history holds another source's payload (`verify` checks it).
De-duplication across fingerprints is a matter of links alone. A row is *hidden* exactly when
readers skip it: a link in force to a row neither marked deleted nor quarantined. Records of one
page are grouped by v2 key and fingerprint, and every group is written onto

1. a **row of its own fingerprint that leaves its reading counted**: one not hidden, or one whose
   hiders show the same **reading** (the payload without the source, times as parsed; for a
   growing interval also the same end - neither an earlier nor a later one); of several, the one
   counted, then live, then already holding this payload, then not superseded by a row of its
   own fingerprint, then the one seen last: a returning record, civil times added, a revised
   value, the old name back (a deletion mark on it is cleared). A spare row of its own that
   fits is reused like any other, so a record that flaps between names or values cycles through
   a few rows instead of adding one each time;
2. else a **new row of its own**, `synthetic:v2s:<hash of key and fingerprint>` (the first free
   spare id of the same kind if that id is taken): a reading Google returns is never hidden
   behind a row showing another value.

A row the page makes **counted** - a new row, or one marked deleted until now - **holds at once**
every counted row of another fingerprint of its device that shows the same reading (a
supersession and its `identity-merged` event, written by the page): a decided rename, a dropped
package suffix or the old name come back is counted once from its first page, and an undecided one
at worst twice. The row Google returned now holds (a later value under its name is written onto
it instead of a new row hidden behind a stale one); of two rows the page writes, the device's
newest form (by its first sighting). A growing interval holds only a row with the same end: a row
showing another end is a value Google no longer returns, counted beside it (at worst twice) until
the complete fetch marks it (above), and a link would keep the row of its own hidden when that
end comes back. A link that would close a cycle of links is left out (both count; the reconciler
lists it). Rows are found by id only: the record's v1 id (rows archived before v2), the `v2s` id of its key
and fingerprint (rows stored since 0012) and its alias, and the `v2s` ids and aliases of the
device's other fingerprints, in one statement; their links (any state, and the links of the rows
those point at) in the next; when no row of its own fits while its `v2s` id is taken, its spare
ids, in blocks of eight until one is free (spare ids are given in order, so the spare rows written
before are found again), and the choice is made again with them. There is no "newest row" fallback and no
scan by start time: an unknown fingerprint (a rename not decided yet, a second device, a late
upload) is counted twice at worst until it is decided, never hidden. Concurrency: every existing
row the page relies on - the rows it writes onto, every row that could hide one of them (the
targets of their links, any state, transitively: a marked one may be revived meanwhile) and the
rows it links - is locked (`FOR UPDATE`, key order) and checked against what the resolution read
(payload, deletion mark, quarantine and links), and new rows are
inserted with `ON CONFLICT DO NOTHING`; when another writer changed, revived, linked or created one
meanwhile, the page is resolved again inside a savepoint (`RECORD_BATCH_RESOLVED_AGAIN`). The time written to
`last_seen_at` is read after the data type's lock was granted, and `last_seen_at` never moves
backwards (`GREATEST`), so a writer that waited cannot make a record look unseen to a fetch that
started meanwhile. The page's sightings (the one row per source that every writer of the data
type shares) and its evidence of two devices are written last, so that row lock is held for as
short as possible.

**Heart-rate zones.** v1 left `heartRateZoneType` out of the key, so the four zone records of one
interval (`LIGHT`, `MODERATE`, `VIGOROUS`, `PEAK`) were written onto one row, each overwriting the
previous one (before `20261020_0011` without a version). v2 keys each zone type apart: from the
deploy on, a re-fetch of such an interval keeps the stored row for the zone type it holds now and
adds rows for the other three. The backfill does nothing more: the stored row holds one real zone
record, its versions (since 0011) hold the others as they were received, and nothing is rewritten;
intervals never re-fetched keep showing one zone type of four.

**Aliases.** A raw row's `google_data_point_id` never changes (trigger), so existing rows are not
re-keyed. `raw_health_data_identity (data_type, identity_key → raw_id, google_data_point_id,
created_at)` points the fingerprint-specific key (`v2s:…`) of every row archived under a v1 id at
that row (the reconciler adds them; the write path adds one when it writes onto such a row), so a
record whose v1 id changed is found again. Rows stored since 0012 need none.

**The reconciler** (`health-mirror identity-backfill`, `app/services/identity_backfill.py`) is the
only place that decides chains, and it links what it decided. A **full** run (after the deploy,
and whenever wanted) reads every row and earlier version (pass 1: sighting spans; rows whose
stored `source_start_time` differs from the parsed one are noted by id), merges the spans into
`raw_source_sightings` (complete from then on), decides the chains, then per v2 key and per device
(one fingerprint or one usable chain) links its rows in two steps: per fingerprint, every row not
yet superseded by one of its own to that fingerprint's **root** (live first, then the one seen
last; a growing interval: the end returned last, then the longest); then every counted root -
live: a row marked deleted counts nowhere, and a link would hide it if its record came back with
another value - to the device's **holder** among them (a growing interval: the end returned
last; then the one the others already link to, the one that already holds others, the one seen
last, the device's newest form), in `raw_health_data_supersessions
(old_raw_id, new_raw_id → data_type, both keys, reason, both fingerprints, same_reading, the
holder's payload hash, created_at)` with the reason `display-name-change`, `package-suffix-change`,
`source-change`, `interval-shape-change`, `growing-interval` or `duplicate`, and appends an
`identity-merged` event for the superseded row (once per row, ever). A root of **another
fingerprint** is linked only when it measured the same (its reading equals the holder's);
otherwise both stay (`kept_differing`). Only roots are linked across fingerprints, so when a chain
is rejected or refuted each fingerprint still counts each record once. The live rows a newly
linked row held are linked to the holder too (rows of the holder's fingerprint, and rows that
measured the same), so two devices joined later end with one holder. Rows whose links are no
longer in force are settled like any other and linked again within each device that remains.

**Agreement.** Per pair of fingerprints with evidence of two devices it also notes, **per key both
hold**, whether some row of each holds the same reading (`raw_source_agreements`, append-only and
guarded like the other identity tables: a row only when that changes, the latest per key is
current) and moves the pair's counts (`identity_reconcile_state.pair_agreement`) by exactly that,
in the same transaction: a key settled again by the overlap, a full run or a decision counts
once, and a night's sample of a few keys never replaces what the archive says. A pair's counts
are **complete** once every key both hold was counted: a full run counts every pair with evidence
when its second pass begins (and sums the counts again from the per-key rows), and the nightly run
first counts the shared keys of every pair established distinct whose counts are not complete (the
start times with rows of more than one source since the later of the two was first seen; reading
rows only, no data type lock). When most keys agree (at least three), the pair is no longer taken
for two devices on that evidence (a rename whose records Google reprocessed) and is listed under
`review_distinct`; a chain is **refuted**, and a deletion marked on that evidence, only when the
counts are complete. `verify` sums the per-key rows again and reports counts that differ. A deleted-marked row with no live row of its device is a deletion:
`deletions_kept` when no other source holds its key live, `deletions_suspicious` when another,
undecided source does (possibly a rename nobody decided: see `ambiguous`). The **incremental**
run (`--incremental`, the nightly scheduler job
`identity-reconcile`, started when the scheduled reconciliations end; see
[Reconciliation](#reconciliation)) decides chains from the sightings as the write path
keeps them and links the start times of rows created or given a new payload (or whose deletion
mark was cleared) since its checkpoint minus one hour, plus exactly the rows whose devices changed:
every row of a link
between two fingerprints that are no longer one device (a refutation; whatever its age, also rows
archived before 0011), and every row first seen since the later of two fingerprints that newly are
was first seen (the start times are read first, in short transactions of their own, as integers;
no snapshot is held while they are settled). Which pairs of fingerprints are no longer, or newly,
one device is written to `identity_reconcile_state.pending_settle` in the transaction that
changes them (a refutation, a new chain, an owner decision) and cleared only when their rows were
settled: a run or decision that fails in between leaves it, and the next run (or decision)
settles it too; a dry run shows it among what it would settle (and writes nothing). A data type
that
never had a full run gets its first full pass instead and says so (`skipped`). A data type that
fails (its lock stays busy, a statement times out) is reported with its `error` and saved in
`identity_reconcile_state`; the run goes on with the others (the CLI then exits 1). Both report
the chains (with their state and review state), the chains awaiting review, the ambiguous sources,
the pairs kept apart (established distinct or not), the pairs that look like a rename although
evidence made them two devices (`review_distinct`), refuted chains, chains whose old form
returned and the rows left double because linking them would hide a live row behind a deleted
one or close a cycle (`conflicts`). A full run
marks every sighting of the data type complete (also a source first stored while it ran).
Checkpoints and the last result per data type, with an `open` section of everything left to
decide, are in `identity_reconcile_state` and in `health-mirror status` (`identity_reconcile`;
`identity_review_suggested` lists the chains to review).

**Deciding by hand** (`health-mirror identity-decide`, `app/services/identity_decisions.py`).
`link FROM TO` (one device; records a chain with `decided_by = owner` naming the decision, or
makes a rejected chain live again), `reject FROM TO` (the chain is wrong: no longer used, its links
no longer hide anything, both sources are free for another chain), `confirm FROM TO` (reviewed; a
rejected chain is live again), `distinct A B` (two devices: a chain between them is no longer used)
and `not-distinct A B` (ignore the evidence of two devices; a chain refuted by it is live again).
Each decision is appended to `raw_source_decisions` (with its time and an optional note) and
applied at once: the rows whose devices it changed are settled again, exactly as the incremental
run settles them (the rows of every lapsed link; every row since the later of two sources newly
joined). A decision records only itself: a chain the rule would decide because of it (a rival
source now known to be another device) is not recorded inside the decision but listed under
`left_to_reconciler` in its outcome, and the next reconciler run decides it like any other (listed
for review), so undoing the decision never meets a chain it made. The latest decision per question
wins, so each decision is undone by the opposite one, in what readers count too (there is no
decision that forgets one). Exactly: `distinct` ↔ `not-distinct`, and `reject` ↔ `link` for a
chain the owner linked. For a chain the reconciler suggested, `confirm` after `reject` makes it
live again but **reviewed** (state `confirmed`, no longer `suggested`): it no longer depends on its
premise (below), and its forms count as reviewed for a `distinct` (deletion marking reaches them).
While the premise holds, readers count exactly as before the `reject`. If the reconciler linked
one of the two freed sources to another one meanwhile, `confirm` is refused, naming that chain;
rejecting it first and confirming makes the original state's counts again, with that chain left
rejected. A chain the
reconciler decided only because another live chain made a rival source continue elsewhere records
that *premise* in its `evidence`; it is live only while the premise is (state `lapsed` otherwise),
unless the owner linked or confirmed it, so undoing the other chain undoes it too. `distinct`
warns, in its outcome and its dry run, that rows of one source whose reading then comes back only
under the other are marked deleted by the next complete fetch: a mark is permanent. A decision that would make live chains fork or cycle
is refused, naming the chain to reject first (for a cycle: the live chains already joining the two,
the first of them to reject); `link` of two sources the owner decided distinct, or
established distinct by evidence, is refused until `not-distinct`. A decision waits up to a minute
for the single-run lock it shares with the reconciler (a nightly batch run), then is refused,
naming who holds it (`healthos-identity-backfill` or `healthos-identity-decide` as the session's
`application_name`) and since when. `--dry-run` reports the effect,
writes nothing and takes no lock that writers wait for; an unknown `--data-type` is named as such.
`health-mirror identity-sources --data-type <type>` lists the sources with their fingerprints,
spans, devices, chains, decisions and open cases. See [docs/RUNBOOK.md](RUNBOOK.md).

A superseded row is **history, not data**: its payload, versions, `source_deleted_at` and events
stay exactly as they were, but while a link in force joins it to a live row, dashboards, Umay, the
`analytics_*` views and the normalized-table readers skip it, as they skip a source deletion, so a
record is counted once. When every such holder is marked deleted (or quarantined), a live
superseded row counts again: a link never hides a live record behind a row that is not counted.
With source deletions included (the dashboard's option), a deleted row is left out when it is
superseded or holds a live row that counts, so a live row's value is shown, never a deleted
holder's (a marked row of another form of the device, not linked to the live one, is shown beside
it in that view). Old deletion marks
are not cleared: they truthfully say that the row's form of the record stopped being returned on
that date; the record itself lives on in the successor row. A marked row with no live row of its
device is a genuine deletion and is never linked (`deletions_kept`), and no marked row is linked
to a row of another fingerprint. Rows of different devices are
never linked. A row may have several links (one no longer in force, then another holder); its
holder is the target of its newest link in force. No link closes a cycle of links (in any state;
`verify` checks it); a link that would is left out (`conflicts`). Rows with a stale stored start are settled last, with the rows of their parsed start
(`start_mismatches`), also when `--after` skipped the rest of the second pass.

The alias, supersession, chain, chain-event, pair, decision, device and agreement tables are
append-only (no `UPDATE`, `DELETE` or `TRUNCATE`, `ENABLE ALWAYS` triggers); `raw_source_sightings`
may only widen (no `DELETE` or `TRUNCATE`; an `UPDATE` may only move `first_seen_at` earlier,
`last_seen_at` and `newest_record_at` later and `complete` to true, never change its key, class,
label or `dataSource`). `health-mirror verify` checks them with the other guards, their keys and checks, that no
rule, row-level security or policy sits on a guarded table, that no guard sits on a table that is
not guarded (a guarded table renamed away), that every `identity-merged` event still has its link,
that the `analytics_*` views are the ones of 0013, and the links and chains themselves: see
[Integrity verification](#integrity-verification). `identity_reconcile_state`,
`identity_deferred_deletions` (one row per deferred deletion; never deleted, `resolved_at` set
when a later complete fetch decides it, or finds its two sources one device) and `fetch_rounds` (when a sync round or backfill window
fetch began at its first page; a resumed fetch keeps it) are bookkeeping.

The dashboard, its history and workouts, and Umay's reads plan every statement with its own
parameters (`plan_cache_mode = force_custom_plan` for their transaction): a generic plan, which
PostgreSQL switches a long-lived connection to after five executions, cannot use the time bounds
and reads many times more rows.

**Concurrency.** `store_records` and deletion marking take a shared transaction lock per data type,
each reconciler batch the exclusive one: it tries for it without queueing for 0.1 s, then waits in
the queue for at most 1 s (a waiting exclusive request queues every new writer behind it, so no
sync waits more than about a second for a batch that is still waiting, even behind a long
transaction); once it holds it, it waits at most 2 s for any row
lock of another transaction (a long import holding rows), then rolls back, waits 2, 4, 8 … s and
retries (`IDENTITY_RECONCILE_LOCK_BUSY`; the CLI prints each retry), and after 8 attempts gives up
on that data type clearly; a sync of that data type waits for one batch (seconds), then continues.
Any long writer of the data type holds it that way: the daily or weekly reconciliation, the
watchdog's recovery after a restart, a sync or backfill by hand. So a run does not give up yet: it
goes on with the other data types (`IDENTITY_RECONCILE_DATA_TYPE_DEFERRED`), then tries the busy
one again, its batches backing off (up to 60 s) until a deadline: the end of the nightly window
(`IDENTITY_RECONCILE_UNTIL_*`, 03:55 UTC) for the nightly job started before it, otherwise
`IDENTITY_RECONCILE_LOCK_WAIT_MINUTES` (60) from its start, also for `identity-backfill` by hand
(`0`: the 8 attempts alone; a `--dry-run` takes no lock). Each attempt keeps the limits above, so
the deadline bounds the retries, never a lock held longer. Only then is the data type failed:
`last_error` in `identity_reconcile_state`, `failed_nights` in `status` and
`identity_lock_contention` in `/health` (below).
A dry run takes no lock at all. Only one reconciler or decision runs at a time: a second
reconciler refuses at once (`another identity-backfill is running`, naming who holds the lock and
since when); the nightly job then skips; a decision waits up to a minute first. Inserts are
chunked (1,000 rows a statement, far below PostgreSQL's 32,767 parameters) for any `--batch-size`.

```sql
-- Bağlanan kopyalar (superseded duplicates) per data type and reason
SELECT data_type, reason, count(*) FROM raw_health_data_supersessions GROUP BY 1, 2 ORDER BY 1, 2;

-- Kaynak zincirleri (source chains: renamed devices, dropped package suffixes)
SELECT data_type, from_label, to_label, decided_by, evidence::json->>'gap_seconds' AS gap_s
FROM raw_source_chains ORDER BY data_type, decided_at;

-- Çürütülen zincirler (chains whose two forms turned out to be two devices; no longer used)
SELECT data_type, from_fingerprint, to_fingerprint, occurred_at FROM raw_source_chain_events;

-- Cihazlar (each chained fingerprint's device now: what readers count supersessions by)
SELECT DISTINCT ON (data_type, fingerprint) data_type, fingerprint, device, decided_at
FROM raw_source_devices ORDER BY data_type, fingerprint, id DESC;

-- Sahibin kararları (the owner's decisions; the latest per question wins)
SELECT data_type, decision, first_label, second_label, note, decided_at
FROM raw_source_decisions ORDER BY id;

-- Bir kopyanın yerine geçen kayıt (the row that holds a duplicate's record now)
SELECT s.reason, n.google_data_point_id, n.raw_json
FROM raw_health_data_supersessions s JOIN raw_health_data n ON n.id = s.new_raw_id
WHERE s.old_google_data_point_id = 'synthetic:…';
```

### Hollow Google names

A Google `name` whose data point id is empty (`users/…/dataTypes/<type>/dataPoints/`) names the
data type, not a record. `respiratory-rate-sleep-summary` arrives so: every sleep's summary under
one name. Such a record is identified by the name plus `@` and its own UTC time
(`app/parsers/common.py`, `source_id`): the start of its interval, or a sample's time. For a
sleep summary that time is the end of the sleep it describes (in the archive it equals a sleep
session's end, or falls inside a session that holds several summaries; one response returns
them all together, so they are separate records, not revisions). The identity is never a
calendar day: sleep by day after a night shift, sleep across midnight and two sleeps of one day
are each their own row; a revised summary of the same sleep is a new version of its row. A
hollow name without any time keeps the bare name.

Before this, every summary was written onto the one row of the bare name: that row holds the
last sleep it received and the earlier sleeps are its [versions](#raw-record-versions). It is
never written, deleted or rewritten. When the row of its own sleep holds exactly its payload,
the write path links the old row to it (`app/services/hollow_names.py`; reason `hollow-name`,
one fingerprint on both sides, so always in force): readers count the sleep once, and a
complete fetch that returns the sleep does not mark the old row deleted. Without such a row
(Google no longer returns that sleep, or returns another value) the old row counts on its own.
`verify` accepts this link and still refuses any other link that hides a record with a Google
id. The sleeps held only in versions are not copied into new rows: Google still returns them
(the next `health-mirror reconcile --days N` stores each as its own row, with its response), and
a derived copy would be a second row of the same record.

**Dating.** Sleep and sleep summaries (`WAKE_DATED_TYPES`, `app/google_health/data_types.py`)
belong to the local day the sleep ended (the wake day): coverage (`retrieval_coverage`) and
Umay's physiology reader count a summary there, wherever the sleep began. Two sleeps that end
on one day are one value day and two records.

### Devices

`GET /api/devices` (System › Cihazlarım; `app/services/devices.py`) lists every source the
archive has seen as one entry per **device**. A device is the set of source fingerprints joined
by live chain links (`raw_source_chains`, as the readers use them), across every data type:
"Google Fitbit Air" and "Fitbit Air" are one watch, shown under its newest name with the older
names in `previous_names`. A link the owner rejected, or one refuted by evidence, keeps the two
forms as two devices. The read is otherwise built from `raw_source_sightings` only: nothing is
written, there is no migration and no new table.

Per device: `name`, `platform`, `form_factor`, `manufacturer`, `application` (the package without
Google's per-install suffix), `recording_method`, `data_types` and `data_type_count`,
`first_seen`/`last_seen` (when the mirror first and last saw any of its forms), `last_data` (the
physical time of its newest record over every data type, `newest_record_at`; a re-fetch of old
records never makes a retired device current) and `status`:

| `status` | Meaning (defaults; `DEVICES_CONNECTED_HOURS`, `DEVICES_DELAYED_DAYS`) |
|---|---|
| `bagli` | newest record at most 48 hours old |
| `gecikmeli` | older than that, at most 7 days |
| `kopuk` | older than 7 days, or no dated record at all |

Every timestamp is UTC ISO 8601; the interface shows them in the chosen time zone with a relative
age ("2 saat önce"). The status describes the archive, not the watch's Bluetooth link: a device
whose records Google has not delivered yet reads `gecikmeli` even while it is worn.

When Google answers, the paired devices (`GET /google-health/devices`) are matched to the archive
devices by name (brand prefix dropped, one name containing the other); a matched device gains
`google_last_sync` and `google_device_type`, the rest are returned in `google_unmatched`, and
`google` is `ok`. When Google fails (no token, HTTP error) `google` is `unavailable` and the
archive's devices are returned all the same; the endpoint never answers 503 for a Google failure.
The response is sent with `Cache-Control: no-store` and requires the UI token like every other
route.

Umay's `health_archive` tool has the same read as the action `devices` (no date range; the
archive only, `google: not_queried`).

The Garmin source is one more entry (`id: garmin`, `provider: GARMIN`, named by
`GARMIN_DEVICE_NAME`) when `garmin_raw_records` holds a record: its `data_types` are the pulled
kinds, `last_seen` the last successful pull (`garmin_last_pull`), `last_data` the newest
normalized Garmin row and `status` follows the same thresholds. Every entry carries `precedence`
([Source precedence](#source-precedence)); the response says `garmin: ok|off`. Apple Health
sources imported directly are entries too, merged into a Google `HEALTH_KIT` device of the same
name ([Apple Health](#apple-health-github-version)); the response says `apple: ok|off`.

## Raw record versions

Google re-sends records with a different payload when it recomputes them (HRV, calories, heart-rate
zones and resting heart rate often change for a day or two). A record is identified by
`(data_type, google_data_point_id)`; when a fetch brings a payload whose `payload_hash` differs
from the stored one, the old payload is **kept as a version**, never overwritten:

- `raw_health_data` holds the current version of every record; every reader (dashboards, Umay,
  normalized tables, `analytics_*` views) keeps using it. `payload_received_at` and
  `payload_source` (`sync`, `webhook`, `reconciliation`, `backfill`) say when and how the current
  payload arrived; both are NULL for payloads stored before migration `20261020_0011`.
- `raw_health_data_versions` holds every earlier payload, numbered per record (1 = oldest), with
  `received_at` (NULL if unknown), `last_seen_at` (the last fetch that still returned it),
  `superseded_at` (the fetch that replaced it), `source` (as above, `gap-repair` for
  `scripts/audit_date_gaps.py --repair`, `legacy` for pre-0011 payloads, `archive-import` for
  payloads recovered from old dumps) and `raw_id` (the current row). A database trigger writes the
  row in the same statement that replaces the payload, whenever `payload_hash` **or** the stored
  text (`raw_json::text`) changes, so a crash, two concurrent writers or a hand-written `UPDATE`
  cannot lose it. Re-receiving the same payload writes nothing. The table is append-only: triggers
  refuse `UPDATE`, `DELETE` and `TRUNCATE`; the one exception is renumbering `version`, which
  PostgreSQL allows only inside a transaction that set `healthos.renumber = '1'` (only
  `health-mirror import-versions` does). `raw_json` is compared as text, so even `1.0` for `1`
  counts as a change.
- **No record is ever deleted.** Triggers on `raw_health_data` refuse `DELETE` (with or without
  history), `TRUNCATE` (also when reached through `TRUNCATE ... CASCADE` from a referencing table)
  and any change of a record's identity (`id`, `data_type`, `google_data_point_id`,
  `first_seen_at`, `created_at`). Every protective trigger is `ENABLE ALWAYS`, so
  `session_replication_role = replica` does not bypass it either, and `health-mirror verify`
  fails if one is missing, disabled, redefined (other events, timing, level, columns or `WHEN`,
  e.g. `WHEN (false)`), joined by another trigger on a guarded table, or its function body was
  replaced. What a trigger cannot stop is the table owner's or a superuser's DDL (`DROP TABLE`,
  `ALTER TABLE ... DISABLE TRIGGER`), and production's `health` role is a superuser; `verify`
  detects the latter, and backups (see below) cover the former (see
  [the runbook](RUNBOOK.md#guards-and-the-database-superuser)).
- Writers lock rows in key order (each page's rows are sorted before the UPSERT), so two
  overlapping pages written at the same time wait for each other instead of deadlocking.
- `source_start_time`, `source_end_time` and `source_timezone` are derived from `raw_json` by the
  parser; they are copied into each version but a recomputation alone (same payload) does not
  create one.
- A record that appears more than once in one page with different payloads becomes one version
  per payload, in page order.
- Normalized tables stay "current state": they are derived from the current raw payload and can be
  rebuilt from `raw_health_data`; the earlier payloads are only in the raw history.
- A record without history rows is version 1, so the migration copied nothing: the existing
  archive is not stored twice and the history grows only by payloads that really changed.

The view **`raw_health_data_history`** lists every version of every record: the history rows plus
the current row, with `version`, `is_current`, `valid_from` (when the version arrived; NULL if
unknown), `valid_to` (when it was replaced; NULL for the current version and for imported versions
whose replacement time is unknown), `last_seen_at`, `source`, `payload_hash` and `raw_json`. Filter
it by `data_type` and `google_data_point_id` (both indexed); for "what changed recently" query
`raw_health_data_versions` directly.

```sql
-- Bir kaydın tüm sürümleri (all versions of one record), oldest first
SELECT version, is_current, valid_from, valid_to, source, payload_hash, raw_json
FROM raw_health_data_history
WHERE data_type = 'heart-rate-variability'
  AND google_data_point_id = 'users/me/dataTypes/heart-rate-variability/dataPoints/…'
ORDER BY version;

-- Son 7 günde değişen kayıtlar (records replaced in the last 7 days)
SELECT v.data_type, v.google_data_point_id, v.version AS replaced_version,
       v.superseded_at, v.payload_hash AS old_hash, r.payload_hash AS current_hash
FROM raw_health_data_versions v
JOIN raw_health_data r ON r.id = v.raw_id
WHERE v.superseded_at >= now() - interval '7 days'
ORDER BY v.superseded_at DESC;

-- Veri türüne göre değişiklik sayısı (changes per data type)
SELECT data_type, count(*) AS earlier_versions, count(DISTINCT raw_id) AS changed_records,
       max(superseded_at) AS last_change
FROM raw_health_data_versions
GROUP BY data_type
ORDER BY earlier_versions DESC;
```

The same from the command line (JSON, oldest version first; no secrets, payloads are included
unless `--no-payload`):

```sh
health-mirror history --data-type heart-rate-variability                 # 20 most recently changed records
health-mirror history --data-type heart-rate-variability --since 2026-10-13 --limit 100
health-mirror history --data-type resting-heart-rate --id 'users/me/dataTypes/…' --no-payload
```

### Recovering versions from old dumps

Before migration `20261020_0011` a changed payload overwrote the stored one; those versions only
survive in older backups. `health-mirror import-versions --from-db <database>` reads a restored
dump (any revision, including `json`-typed `raw_json` from before `20261020_0010`) and merges each
record's dump rows into its timeline by `last_seen_at`:

- A dump payload is *already present* only when it lands next to the same payload in that
  timeline. A payload that left and came back (A → B → A) therefore stays three versions; it is
  never deduplicated against an older, non-adjacent version.
- New payloads become versions with `source = 'archive-import'` and `superseded_at` set (the
  dump's own value when it has one, otherwise the payload's last sighting), so
  `history --since` lists them. Existing history rows keep their order and are only renumbered.
- A record the live archive does not have at all is **imported as its own row** (`payload_source =
  'archive-import'`, `source_deleted_at` set to the dump's value or the import time, so dashboards
  skip it), its latest dump payload as current version and earlier ones as history. Nothing that
  ever existed is lost. Live current rows are never changed; an import-restored row may take a
  newer payload from a later dump (the trigger keeps the old one).
- Rows whose stored hash does not match their payload (`source_hash_mismatches`) and payloads
  jsonb cannot store unchanged (`unrepresentable`: a float written as `1e+16`, `-0.0`, a `\u0000`
  escape) are skipped and listed, without aborting the rest; `--dry-run` reports both too, and the
  command exits 1 so the dump is kept.

Running it again adds nothing. Dumps are lossless when imported **newest first** (an older
sighting then always lands at the old end of a timeline); `--from-db` may be repeated and the
command sorts the dumps that way itself. `--from-db` takes a database name on the live server or a
full SQLAlchemy URL. Procedure: [RUNBOOK](RUNBOOK.md#import-payload-versions-from-archive-dumps).

## What is preserved where

Nothing received from Google, and nothing entered by hand, is deleted or overwritten. Storage cost
is not a concern; every guard below is a database trigger, so it holds for every code path, the
CLI and hand-written SQL alike (`health-mirror verify` checks that the guards are in place).

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
| Exact HTTP response bodies from the Garmin Connect Personal API ([below](#garmin-connect-personal-api)) | `garmin_api_responses` | append-only (trigger); SHA-256 of the stored bytes |
| Every distinct payload of a Garmin record (per kind and calendar day or activity) | `garmin_raw_records` | append-only (trigger); a changed payload is a new row, the earlier row stays; readers take the newest |
| Garmin rows of the normalized tables (`provider = 'GARMIN'`, key `garmin:<kind>:<key>`) | `steps`, `heart_rate`, `sleep_sessions`, `exercise_sessions`, … | rebuilt from `garmin_raw_records` by comparison (unchanged rows untouched, changed rows updated in place); the raw archive stays the truth |
| Health Auto Export request bodies ([Apple Health](#apple-health-github-version)) | `apple_payloads` | append-only (trigger); one row per distinct body (SHA-256), never `Authorization` |
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
  `health-mirror responses` / `export-response`), and only the parser sees the decoded JSON.
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
health-mirror responses --data-type heart-rate --since 2026-10-20 --limit 5   # metadata, newest first
health-mirror responses --context webhook:<element hash>
health-mirror export-response 12345 --output page.json   # the exact bytes; metadata on stderr
sha256sum page.json                                      # equals body_sha256
```

`export-response` refuses (exit 1) when the stored bytes no longer match their SHA-256.

### Records jsonb cannot hold

PostgreSQL `jsonb` holds neither U+0000 nor an unpaired surrogate nor a non-finite number, all of
which Google's JSON can carry (`\u0000`, `\ud800`, `NaN`, `1e400`). One such record used to fail
its page on every attempt, so its data type never advanced. `store_records` now checks every
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
health-mirror rejections                 # newest first, metadata only, with the ack in force (JSON)
health-mirror rejections --element 7     # the parsed record of rejection 7 on stdout
health-mirror export-response 12345      # the exchange it came from, byte for byte
health-mirror rejections --ack 7         # every rejection up to 7 was looked at
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
therefore keeps its whole timeline; `health-mirror history` lists the events next to the
versions. `identity-merged` is appended (by `health-mirror identity-backfill`, or by the page
that creates a row of a decided device and links it; not by a trigger) when a duplicate row is
linked to the row that holds its record now (see
[Record identity](#record-identity)); it changes nothing on the row.

### Write paths (audit)

Every `UPDATE`, UPSERT and `DELETE` in `app/`, `scripts/` and `migrations/` that touches a table
above, and why it is allowed:

| Write | Table | Verdict |
|---|---|---|
| `store_records` UPSERT (`app/services/storage.py`) | `raw_health_data` | onto the row that holds the record's identity (never re-keyed); payload change → kept as a version by trigger first; `last_seen_at`, `source_deleted_at` → `NULL` (logged as an event), `payload_received_at`/`payload_source`/`response_id` change only with the payload; derived `source_*_time`/`source_timezone` |
| `store_records` aliases, evidence and links of its new rows (`app/services/identity.py`, `source_chains.py`) | `raw_health_data_identity`, `raw_source_pairs`, `raw_health_data_supersessions`, `raw_health_data_events` (`identity-merged`) | `INSERT ... ON CONFLICT DO NOTHING` / inserts only |
| `store_records` sightings (`app/services/source_chains.py`) | `raw_source_sightings` | first/last sighting widened in place (bookkeeping, no received data) |
| `identity-backfill`, the nightly `identity-reconcile` job and `identity-decide` (`app/services/identity_backfill.py`, `identity_decisions.py`) | `raw_health_data_identity`, `raw_health_data_supersessions`, `raw_source_chains`, `raw_source_chain_events`, `raw_source_pairs`, `raw_source_decisions`, `raw_source_devices`, `raw_health_data_events`, `raw_source_sightings`, `identity_reconcile_state` | inserts only (sightings widened, marked complete); `raw_health_data` and its versions are only read |
| `mark_source_deletions` (`app/sync/deletions.py`) | `raw_health_data` | sets `source_deleted_at` only (logged as an event); nothing deleted |
| import-restored row takes a newer dump payload (`app/services/raw_history.py`) | `raw_health_data` | only rows the import itself created; the old payload is kept as a version by trigger |
| version renumbering (`import-versions`) | `raw_health_data_versions` | `version` only, in a transaction with `healthos.renumber = '1'` |
| quarantine of rollup records (migration `0002`, applied long ago) | `raw_health_data` | flag columns only, payload untouched |
| batch/element processing (`app/services/webhook_processing.py`) | `webhook_events`, `webhook_notifications` | operational: `status`, counters, `last_error`, `processed_at`, `failure_count`, `element_count`, filling a missing `raw_body`/`signature_verified_at` once; payload frozen by trigger |
| test-entry flag and its removal (`app/services/manual_flags.py`) | `manual_measurement_flag_events` | inserts only; `manual_measurements` is only read |
| `signature_verified_at` backfill (migration `0003`) | `webhook_events` | operational, fills a NULL |
| sync state, backfill jobs, reliability state, appearance, display preferences, OAuth token rotation | `sync_state`, `backfill_jobs`, `reliability_state`, `appearance_settings`, `display_preferences`, `oauth_tokens` | operational metadata, not received data |
| normalized UPSERTs | `heart_rate`, `steps`, … | derived, see below |
| `downgrade()` of migrations | — | refuses while versions, responses or events exist (`-x allow-version-loss=1` after a backup) |

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
`valid_sources_only` flag, kept for older callers, is now the default and changes nothing. A
rebuild of the normalized tables from `raw_health_data` therefore gives the same counts, and
nothing has to be kept in sync. A renamed record written after `20261021_0012` updates its existing normalized row (same
key), so no new duplicate appears.

## Webhooks

Configure a Google Health subscriber with:

- `endpointUri`: `https://your-domain.example/webhooks/google-health`
- `endpointAuthorization.secret`: an unpredictable exact value also set as `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`
- automatic subscription policies for the data types you consented to
- the **Google Cloud project number**, not its textual project ID

The endpoint rejects an unauthenticated verification challenge, accepts the authenticated
verification body, then requires both the authorization secret and Google's ECDSA signature for
notifications. Google may send either one notification object or an array containing many
notifications. The signed request is preserved as an immutable batch, future receipts retain the
exact raw request bytes, and every array element gets independent durable extraction, authorized
health-user validation, global content-hash deduplication, processing state, and retry history.
Returning HTTP 204 proves durable acceptance only; `processed_at` is set after every valid element
in the batch completes. A failed element remains retryable without re-running successful elements.
The documented deployment has one subscriber, `personal-health-mirror`, registered using
short-lived service-account impersonation; no service-account key is stored. The project number is
deployment-secret metadata and is not repeated in tracked documentation.

Run `health-mirror webhook-readiness` before subscriber registration. It reports only booleans and
never prints the endpoint authorization value. Registration is ready only when the project number
is numeric, the endpoint uses HTTPS, the authorization value includes a `Bearer` or `Basic` scheme,
and the subscriber ID matches Google's naming rules.

### Cloudflare Tunnel receiver (deployed)

The deployment uses a host-level `cloudflared` service on the VM. It makes outbound-only
connections, so the router has no inbound port forwarding. The tunnel route must match
`${CLOUDFLARE_WEBHOOK_HOSTNAME}` and path `^/webhooks/google-health$`, forward to
`http://127.0.0.1:8000`, and use a final catch-all 404 rule. Only the tunnel-scoped credential
file lives on the VM; no account-wide `cert.pem` or router fallback is used.

The app remains bound to loopback, PostgreSQL has no published port, and the path rule prevents
public access to `/health`, `/docs`, OAuth, sync, backfill, or Google Health metadata routes.
Setup and the public-surface checks are in [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md). Local
status:

**Verification-only command:**

```sh
systemctl status cloudflared
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
docker compose exec -T app health-mirror status
docker compose exec -T app health-mirror webhook-readiness
docker compose exec -T app health-mirror webhook-status
```

After deploying a fix or recovering from an outage, retry stored `pending` and `failed` elements:

**Operates only on already verified stored webhook batches:**

```sh
docker compose exec -T app health-mirror webhook-replay-pending
```

Despite its name, this command retries `failed` elements too; completed elements are not re-fetched.
It reuses verified batches and read-only Google queries; no SQL reset or Google resend is needed.
A live re-delivery of a signed body that was first verified more than 24 hours earlier is accepted
(HTTP 204) but not processed again (`WEBHOOK_REPLAY_IGNORED`); the operator command above is not
limited.

`cloudflared` runs with `--no-autoupdate`; update it deliberately (see the
[runbook](RUNBOOK.md#update-images-and-cloudflared)). Google retries failed webhook notifications
for up to seven days; daily and weekly reconciliation remain the recovery layer for longer outages.
A diagnosis checklist is in the [runbook](RUNBOOK.md#the-webhook-went-quiet).

### Direct Caddy receiver (unused alternative)

The backend validates `Host` for HTTP and WebSocket requests. Private routes accept only
`localhost`, `127.0.0.1`, or `[::1]`, with optional ports. The HTTPS hostname in
`GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` additionally accepts only POST requests to
`/webhooks/google-health`; webhook authorization and signature checks still apply.
Proxies must preserve the original Host header. `APP_HOST` controls listening only and does
not grant access to additional hostnames.

Webhook authorization is checked before reading the body. Payloads exceeding
`GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES` (default 2 MiB, configurable from 1 KiB to 16 MiB)
return 413, including chunked requests and verification payloads.

Job-start POST routes (`/sync/all`, `/sync/{data_type}`, `/backfill/start`) require
`X-HealthOS-Request: 1`. Browser requests must also have a same-origin Origin when supplied;
cross-site requests are rejected with 403. The UI includes the header automatically.
Command-line clients can omit Origin but must send the explicit header. Keep CORS disabled
for these local routes; the custom header prevents cross-site forms and forces a browser
preflight for cross-origin fetches.

The optional `webhook` Compose profile runs Caddy on the same host and forwards only
`/webhooks/google-health` to the private app container; every other public path returns 404.
Caddy obtains and renews a public TLS certificate without a paid cloud service.

1. Choose a DNS hostname (an owned domain or a free dynamic-DNS hostname) and point it directly to
   this host's public IP.
2. Confirm the connection is not behind carrier-grade NAT, then forward router TCP ports 80 and 443
   to this host. UDP 443 is optional and enables HTTP/3.
3. Set `WEBHOOK_HOST` to the hostname and set
   `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI=https://<hostname>/webhooks/google-health` in `.env`.
4. Start the profile with `docker compose --profile webhook up -d caddy` and verify both Google's
   authenticated-success and unauthenticated-rejection handshakes before registering the subscriber.

This alternative is direct self-hosting: health webhook traffic is not routed through a tunnel or hosted proxy.
If the internet connection uses carrier-grade NAT or blocks inbound TCP 80/443, obtain a public IP
or explicitly choose another privacy-compatible ingress method before registration.

## Reconciliation

Defaults:

- Daily at 02:00 UTC: re-read the last 7 days.
- Sunday at 03:00 UTC: re-read the last 90 days.
- When the scheduled reconciliations have ended (the daily run, and on Sunday the weekly one
  too: whichever ends last; also when one fails or is missed): the identity reconciler
  (`identity-backfill --incremental`, job `identity-reconcile`, `IDENTITY_RECONCILE_CHAINED` in
  the log). It decides renamed sources and links the duplicate rows written since its last run,
  and lists what it leaves to the owner in `health-mirror status`
  ([Record identity](#record-identity)). A reconciliation holds each data type's shared lock while
  it stores pages and checks deletions (heart-rate for 20 to 60 minutes of the 7-day run, more
  than four hours of the 90-day one), so a reconciler at a fixed 02:30 UTC met it night after
  night. A data type still busy (a long webhook sync, a watchdog recovery) is retried after the
  others until 03:55 UTC (`IDENTITY_RECONCILE_UNTIL_*`; started later, for
  `IDENTITY_RECONCILE_LOCK_WAIT_MINUTES`). With `RECONCILIATION_ENABLED=false` it runs at
  `IDENTITY_RECONCILE_HOUR_UTC`:`IDENTITY_RECONCILE_MINUTE` (02:30 UTC) instead. It may overlap
  the 03:30 (local) backup: `pg_dump` takes only `ACCESS SHARE` locks, which none of its inserts,
  updates or `ANALYZE` conflicts with, and dumps one snapshot (what the reconciler commits later
  is in the next backup), as with the reconciliations that already run then.

These use normal idempotent UPSERTs. Disable in `.env` when another scheduler instance owns the jobs.
The derived-day refresh (every five minutes) has no switch, so the scheduler keeps running and
`/health` reports `scheduler: running` even with all three jobs above disabled.
Run either depth manually with `health-mirror reconcile --days 7` or
`health-mirror reconcile --days 90`.

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
cannot be queried, allowing Docker to restart a genuinely unusable app without creating restart
loops for upstream staleness. Otherwise it returns HTTP 200 with `status` equal to `healthy`,
`degraded`, `stale`, or `failed`, plus database/scheduler/configuration state, webhook and sync
timestamps, pending/failed counts, stale stream names, reasons, latest raw write, last successful
reconciliation, and per-stream source freshness. `stale` means all frequent streams crossed the
configured age; `degraded` covers partial staleness, backlog, incomplete configuration, or a stopped
scheduler; `failed` reports unresolved failed webhook or sync state.

`identity_lock_contention` lists the data types whose last identity reconciliation gave up on a
busy lock: `data_type`, `nights` (UTC nights since its last successful run, its checkpoint `since`;
with one nightly run a night, the nights in a row it failed) and `last_attempt`. It is a warning
and never changes `status` (the data type's records are stored and counted; only the linking of
renamed sources waits); empty once a run settles it. `health-mirror status` shows the same count
for any error as `failed_nights` beside each data type's `last_error`.

After a host interruption the expected recovery sequence is:

1. The VM boots and systemd starts `docker.service` and `cloudflared.service`.
2. Docker starts the existing `postgres` and `app` containers under `unless-stopped`.
3. Compose waits for PostgreSQL's `pg_isready` health check; the app then runs migrations and its
   own database readiness query before starting the scheduler.
4. The startup watchdog assesses the persisted gap and, when eligible, starts one bounded
   reconciliation. `cloudflared` forwards webhooks again as soon as port 8000 answers; until then
   Cloudflare returns 502 and Google retries.
5. If installed and enabled, `healthos-bridge.service` starts the optional Umay host bridge;
   otherwise use the manual launcher. See the [bridge runbook](RUNBOOK.md#restart-the-umay-bridge).

A sync state or webhook element left `running` by a process that died is failed (and so becomes
retryable) at startup, but only once nothing has touched it for 15 minutes: a running fetch refreshes
its sync state on every page, so a fresher row may be a `health-mirror sync` or webhook replay still
working in another container. Rows that were fresh at startup are re-checked once the 15 minutes
have passed, and the watchdog likewise ignores `running` rows without a recent heartbeat.

If Docker itself is down, no in-container watchdog can notice. That host-level condition is visible
as a Cloudflare 502 on the webhook and must be handled by host monitoring.

## Garmin Connect Personal API

Optional and off by default (`GARMIN_ENABLED=false`). Garmin's Personal API is a read-only,
pull-only source for the owner's own Garmin Connect account: there is no webhook, no OAuth
consent screen and no write endpoint. HealthOS treats it as a **second source next to Google
Health**, in its own tables; nothing Google-shaped is touched. The exception to the "no new
external services" rule was decided by the maintainer on 7 November 2026 (`AGENTS.md`, rule 6).

**Key and token.** The Personal API key (created in Garmin Connect, valid for a limited period;
Garmin e-mails a week before it expires) lives in a file, `GARMIN_API_KEY_FILE` (default
`~/.config/garmin/connect-personal-api-key`, mode 600), and is read from that file at every
token exchange. It is never put in `.env`, the database, a log, a response row or an error
message. The key is exchanged for a bearer token (`POST
/personalapitoken-service/personal-api/personal-api-key/token`, `Authorization: ApiKey …`; the
answer is the token as plain text, a JWT valid for 24 hours, no refresh). The token is kept in
memory only and renewed an hour before it expires or once when a request answers 401; a second
401 fails the pull. To renew the key, replace the file's content; nothing restarts. In Compose,
mount the file read-only and point `GARMIN_API_KEY_FILE` at the mount (commented example in
`docker-compose.yml`).

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

**Archive.** Every HTTP exchange (token exchange included, retries and error statuses too) is
kept byte for byte in `garmin_api_responses` (`body`, `body_sha256`, request `path` and
`query`, never a header). Each record of a response is kept in `garmin_raw_records` under its
kind and key (the calendar day, or the activity id) with the SHA-256 of its canonical JSON:
fetching a day again with the same content adds nothing (unique `(kind, key, body_sha256)`),
a changed content is a new row with its own `first_seen_at` and the earlier row stays. Readers
take the newest row per kind and key. Both tables refuse `UPDATE`, `DELETE` and `TRUNCATE`
(migration `20261107_0024`, triggers checked by `health-mirror verify`); a record PostgreSQL
`jsonb` cannot hold is counted (`GARMIN_RECORDS_UNSTORABLE`) and left in its response row
only. `garmin_pull_state` is bookkeeping: per kind the last attempt, success, error and the
backfill checkpoint.

**Schedule.** With `GARMIN_ENABLED=true` the scheduler job `garmin-pull` fetches the last
`GARMIN_LOOKBACK_DAYS` (default 3) up to today every `GARMIN_PULL_INTERVAL_MINUTES` (default
120), at :10 past the hour and never between 02:00 and 03:59 UTC (Google reconciliation,
identity reconciler, backup). A failed kind is logged (`GARMIN_PULL_KIND_FAILED`), written to
`garmin_pull_state.last_error` and shown by `GET /api/garmin/status`; the other kinds go on and
the next run fetches the same window again. With the defaults a run makes 23 requests.

```sh
health-mirror garmin-status                                  # enabled, key file present, last success/error, per kind
health-mirror garmin-pull [--kind steps --kind sleep]        # one incremental run
health-mirror garmin-backfill --since 2026-06-01 [--until 2026-10-31] [--dry-run] [--kind …]
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
(`precedence`, `policy`; [Source precedence](#source-precedence)).

**Normalization (phase 2).** After every pull and at the end of every backfill the window's
newest records are normalized into the *shared* normalized tables, tagged
`provider = 'GARMIN'` (`app/services/garmin_normalize.py`, migration `20261107_0025`):

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
(`app/sync/deletions.py`) reads `raw_health_data` only, so a complete Google fetch never marks
a Garmin row (tested: `tests/test_garmin_normalize.py`). The identity layer keys sources by
`platform`, so a `GARMIN` source could never chain to the Fitbit. Garmin's own pull state and
freshness are in `GET /api/garmin/status` and the Cihazlarım entry.

## Apple Health (GitHub version)

Apple Watch and iPhone data can be brought into HealthOS directly, through two gates. Both are
part of the published (GitHub) version only; they are kept on a separate branch (`eklenti/apple`,
see [Branches](BRANCHES.md)) and are not installed on the maintainer's own server, which receives
Apple data, if at all, through Google Health (`HEALTH_KIT` records). Neither gate is a new external
service: HealthOS fetches nothing from Apple. Gate 2 is a file the owner exports from the Health
app; gate 1 is a request the owner's own phone sends (Health Auto Export is an app on the phone,
not a cloud API), so the rule against new external services is not touched.

| Gate | What arrives | Default | Endpoint |
|---|---|---|---|
| 1. Health Auto Export receiver | JSON from the [Health Auto Export](https://help.healthyapps.dev/en/health-auto-export/export-format) iOS app's *REST API* automation (paid feature of that app) | **off** | `POST /webhooks/apple-health` |
| 2. Manual export upload | the Health app's `export.zip` (or the `export.xml` inside it) | available | System › "Apple Sağlık içe aktar", `POST /api/apple/import`, or `health-mirror apple-import PATH` |

**Gate 2: export.zip.** In the Health app: profile picture › *Export All Health Data*. The
archive is a zip with `apple_health_export/export.xml` (its name is localized in some languages;
the importer finds the file by its content: the first `.xml` entry other than `export_cda.xml`
whose first 64 KiB contain `<HealthData`), `export_cda.xml`, workout routes (`.gpx`) and ECG
files. Only the main XML is read; the other entries are skipped (their file names are kept in a
workout's `routes`). The upload is streamed: the zip is read in arrival order (local file headers,
deflate or stored, zip64), the XML is parsed with an expat push parser (no tree is built, memory
stays constant: a 50 MB synthetic stream peaks at a few MB in the tests), and every 2,000 records
are archived, normalized and committed. Nothing is written to disk or held whole in memory, so a
multi-gigabyte `export.xml` works; the request simply runs until the file is read. The XML's
internal DTD (ELEMENT/ATTLIST declarations) is accepted; any `<!ENTITY` declaration, an external
DTD and undeclared entities are refused (no entity expansion, no external fetch). Limits:
`APPLE_IMPORT_MAX_UPLOAD_BYTES` (default 8 GiB) for the upload, 64 GiB decompressed per zip
(zip-bomb guard); encrypted entries and stored entries with a data descriptor are refused. One
import runs at a time per process (a second upload answers 409). If the upload breaks off, the
records committed so far stay and the import row is marked `failed`; **uploading the same export
again (or a newer one) adds only what is missing**: every record version is stored once.

From `export.xml`: every `<Record>` (quantity, category and other types, with its
`MetadataEntry` children and HRV beat-to-beat lists), `<Correlation>` (blood pressure; its child
records are also records of their own), `<Workout>` (attributes, metadata, events, statistics,
route file names; unknown nested elements under `children`) and `<ActivitySummary>`. `<Me>` (date
of birth, sex, blood type), `<ExportDate>` (kept as the import's `export_date`),
`ClinicalRecord`, `Audiogram`, `VisionPrescription` and other top-level elements are counted as
skipped, not stored.

**Gate 1: Health Auto Export.** The receiver is off unless `APPLE_RECEIVER_ENABLED=true`, and even
then it answers only requests carrying `Authorization: Bearer <token>` with the token kept in
`APPLE_RECEIVER_TOKEN_FILE` (default `~/.config/healthos/apple-receiver-token`, at least 32
characters, read at every request, so replacing the file rotates it without a restart; never in
`.env`, the database or a log). `health-mirror apple-token` writes a new token (directory 700, file
600; `--rotate` to replace one, `--file PATH` for another location) and prints the header line
once. The route is exempt from `APP_UI_TOKEN` (it has its own token) and from no other check:

| Order | Check | Answer |
|---|---|---|
| 1 | `APPLE_RECEIVER_ENABLED` is false | 404 |
| 2 | request budget, authorized and unauthorized counted apart (`APPLE_RECEIVER_REQUESTS_PER_MINUTE`, default 30 each) | 429 |
| 3 | token file missing, unreadable or shorter than 32 characters | 503 |
| 4 | no single `Authorization: Bearer` header equal to the token (constant-time comparison) | 401 |
| 5 | body over `APPLE_RECEIVER_MAX_BODY_BYTES` (default 32 MiB; use the app's *Batch Requests* for large ranges) | 413 |
| 6 | body not strict JSON (UTF-8, no duplicate keys, no NaN/Infinity) or no `data` object | 400 (the authenticated body is still kept) |
| — | otherwise | 200 `{"status": "ok" \| "duplicate", "records", "new", "rows", "skipped", "invalid"}` |

Every authenticated body is kept byte for byte in `apple_payloads` with its SHA-256 and the app's
descriptive headers (`automation-name`, `automation-id`, `automation-aggregation`,
`automation-period`, `session-id`, `content-type`, `user-agent`; never `Authorization`); a body
sent again is not stored twice and answers `duplicate`. From `data.metrics[]` every data point
becomes a record (`{metric, units, aggregation, point}`), from `data.workouts[]` every workout
(version 1 or 2); `stateOfMind`, `ecg`, `medications`, `symptoms`, `cycleTracking`,
`heartRateNotifications` stay in the stored body only and are counted as skipped. A metric
point's identity is its metric, the automation's aggregation, its dates and its source: when the
app re-sends a running day total with a larger value, the new value is a new version of the same
record and the readers take the newest.

> [!NOTE]
> The receiver follows Health Auto Export's published format
> ([export format](https://help.healthyapps.dev/en/health-auto-export/export-format),
> [health metrics](https://help.healthyapps.dev/en/health-auto-export/export-format/health-metrics),
> [workouts](https://help.healthyapps.dev/en/health-auto-export/export-format/workouts),
> [REST API automation](https://help.healthyapps.dev/en/health-auto-export/automations/rest-api)).
> It has not been tested against a payload from a real phone: the test fixtures are synthetic,
> built from the documented field names. Metric names the documentation does not list
> explicitly (`resting_heart_rate`, `heart_rate_variability`, `blood_oxygen_saturation`,
> `weight_body_mass`, …) follow the app's snake_case naming; an unrecognized name is still
> archived and normalized into `scalar_metrics` as `apple:<name>`, never dropped. Reports from
> real use are welcome.

**Reaching the receiver.** The app stays bound to `127.0.0.1` (rule 4). The phone needs an https
address, which the owner provides with their own tunnel or reverse proxy that forwards **only**
`POST /webhooks/apple-health` to `127.0.0.1:8000` and keeps the public `Host` header (the
[Cloudflare Tunnel](CLOUDFLARE_TUNNEL.md) setup of the Google webhook works the same way with an
ingress rule for this path; Caddy or Tailscale Funnel work too). Set `APPLE_RECEIVER_ENDPOINT_URI`
to that https URL (it must end in `/webhooks/apple-health`): the host validation then admits that
host for `POST /webhooks/apple-health` only; every other path and method from it answers 400, and
a request reaching the receiver over loopback works without it. In Compose, mount the token file
read-only and point `APPLE_RECEIVER_TOKEN_FILE` at the mount (commented example in
`docker-compose.yml`); the file must be readable by the container user (uid 10001), e.g.
`chown 10001 <file> && chmod 600 <file>` on the host.

**Health Auto Export settings** (Automations › new › *REST API*): URL = the endpoint URI;
*HTTP Headers*: `Authorization` = `Bearer <token>`; *Export Format*: JSON; *Export Version*: 2;
*Data Type*: Health Metrics (a second automation for Workouts); *Summarize Data*: on (smaller
requests; sleep then arrives as one night per point) or off (raw samples and sleep stages);
*Time Grouping*: minutes or hours; *Date Range*: Since Last Sync; *Batch Requests*: on. The app's
run log (*View Activity Logs*) shows the receiver's answer.

**Tables** (migration `20261107_a001`, only new tables): `apple_payloads` and `apple_raw_records`
are append-only (UPDATE/DELETE/TRUNCATE refused by trigger, checked by `health-mirror verify`);
`apple_raw_records` holds one row per record version (unique channel, kind, key and body
SHA-256; `channel` `export` or `hae`, `kind` `quantity`/`category`/`record`/`workout`/
`activity-summary`/`hae-metric`/`hae-workout`, `type` the HealthKit identifier or the metric
name, the body as received, source name, device model, start and end). `apple_imports` (status,
progress and counts of each upload, CLI import or receiver request) and `apple_sources` (per
Health source: device model, first and last import, newest data, data types, channels) are
bookkeeping. The downgrade refuses while either archive table holds a row.

**Normalization.** Records go into the shared normalized tables with `provider = 'APPLE'`
(`app/services/apple_normalize.py`), idempotent by comparison like Garmin's: a second run writes
nothing; when several versions of one record arrive, the newest wins.
`health-mirror apple-normalize [--since YYYY-MM-DD]` rebuilds the rows from the archive.

| Apple type (export identifier without `HK…TypeIdentifier` / Health Auto Export name) | Table | Notes |
|---|---|---|
| `StepCount` / `step_count` | `steps` | |
| `DistanceWalkingRunning`, `DistanceCycling`, `DistanceSwimming`, `DistanceWheelchair`, `DistanceDownhillSnowSports` / `*_distance` | `distance` | metres (from m, km, mi, ft, yd, cm) |
| `ActiveEnergyBurned` / `active_energy`, `BasalEnergyBurned` / `basal_energy_burned` | `energy_expenditure` | kcal (from kcal, Cal, kJ, J, cal); active and basal told apart by the type in `source_record_id` |
| `HeartRate` / `heart_rate` | `heart_rate` | Health Auto Export: `Avg` (Min/Max stay in the archive) |
| `RestingHeartRate` / `resting_heart_rate` | `resting_heart_rate` | |
| `HeartRateVariabilitySDNN` / `heart_rate_variability` | `heart_rate_variability` | **SDNN, not RMSSD**: `rmssd` stays NULL, the value is `metrics.sdnn_ms` |
| `OxygenSaturation` / `blood_oxygen_saturation` | `oxygen_saturation` | percent (a fraction ≤ 1 is multiplied by 100) |
| `RespiratoryRate`, `VO2Max`, `BodyMass` | `respiratory_rate`, `vo2_max`, `weight` | weight in kg (from lb, g, oz, st) |
| `BodyTemperature`, `AppleSleepingWristTemperature` | `temperature` | °C (from °F) |
| `SleepAnalysis` / `sleep_analysis` (export, and Health Auto Export with Summarize Data off) | `sleep_sessions` | one row per segment; the stage (`core`, `deep`, `rem`, `awake`, `asleep`, `in_bed`) is the last part of `source_record_id` |
| `sleep_analysis` with Summarize Data on | `sleep_sessions` | one row per night (`sleepStart`–`sleepEnd`, else the in-bed times), stage hours as seconds |
| `Workout` / Health Auto Export workouts (v1, v2) | `exercise_sessions` | type without `HKWorkoutActivityType`, or the app's name; calories and distance from the attributes, the workout statistics or the app's `activeEnergyBurned`/`distance` |
| every other quantity or metric, `ActivitySummary`, other category records, blood pressure | `scalar_metrics` as `apple:<type>[:<field>]` | with its unit; nothing silently dropped |

A record without a usable time or value is counted as skipped; correlations are skipped because
their child records are normalized themselves. An Apple row's `google_data_point_id` is
`apple:<channel>:<kind>:<key>[:<part>]` (no Google id can equal it), `source_record_id`
`<channel>:<type>:<key>[:<part>]`, `device` the Health source name (`sourceName`; the model is in
`apple_sources` and Cihazlarım) and `source_timezone` the record's UTC offset. As for Garmin,
every Google reader stays Google-only (an Apple row has no `raw_health_data` row).

**No double counting.** Apple's export holds overlapping samples from several sources (the Watch
and the iPhone both count steps), and an owner may use both gates. The Apple readers
(`app/services/apple_readers.py`) therefore never add sources or gates together:

- steps: per gate, each sample is split proportionally into local hours; per hour the single
  source with the most steps counts; the day is the sum of its hours and the larger of the two
  gates' totals;
- sleep: per gate and source, the union of asleep segments only (never in-bed or awake);
  segments less than 3 hours apart are one night, assigned to the local day it ends (the
  dashboard's rule); a summarized night counts by its `totalSleep`; the source with the most
  sleep, then the larger gate;
- resting heart rate: the newest record of the local day; HRV (SDNN) and SpO₂: the daily mean,
  one sample per timestamp across both gates; heart-rate samples: one per timestamp;
- workouts: an export workout and a Health Auto Export workout overlapping by at least half of
  the shorter one are one workout (the export copy is kept).

**Precedence and Cihazlarım.** Apple values take part in [Source precedence](#source-precedence)
with the source code `HEALTH_KIT` (shown as "Apple Sağlık"), the same code Google uses for the
Apple data it relays, so the policy has one name for Apple whichever way the data came; `provider`
(`APPLE` or `GOOGLE`) tells the ways apart. When Google relays a `HEALTH_KIT` value for the same
day, Google's is shown first and the direct Apple value is its alternative (never summed); the
hourly heart chart takes Apple samples only in minutes Google's `HEALTH_KIT` samples leave empty,
and an Apple workout overlapping a Google `HEALTH_KIT` workout by at least half of the shorter one
is folded under it as `secondary`. Cihazlarım lists every Health source (`id: apple-…`,
`provider: APPLE`, `source_code: HEALTH_KIT`, the device model, the last import and the newest
data); a source whose name matches a Google `HEALTH_KIT` device (the same comparison as Google's
paired devices: brand prefix dropped, one name containing the other) is merged into that row
(`providers: ["GOOGLE", "APPLE"]`, the newer `last_data`); names that do not match, for example
after a device was renamed, stay two rows. The response says `apple: ok|off`. Umay's
`health_archive` reads the same values (`daily`, `devices`).

**Status and CLI.**

```sh
health-mirror apple-token [--rotate] [--file PATH]   # receiver token: written once, printed once
health-mirror apple-import PATH                      # export.zip or export.xml (native installs; no size limit)
health-mirror apple-status                           # receiver settings and the last 10 imports (JSON)
health-mirror apple-normalize [--since YYYY-MM-DD]   # rebuild the normalized Apple rows from the archive
```

`GET /api/apple/status` (UI token) returns `receiver` (`enabled`, `token_file_present`,
`endpoint_configured`, `path`), `running` (the live progress of an import in this process, or
null), the last ten `imports` and the Health `sources`; no record values, no secrets. In Docker
prefer the System page upload (the container's `/tmp` is a memory-backed tmpfs and its
filesystem is read-only); an import row left `running` by a crash stays so (it is not failed at
startup, which could wrongly fail a concurrent CLI import).

**Keeping the Apple branch current (maintainers).** `eklenti/apple` takes `main` by merge, never
rebase. Migrations: `20261107_a001` follows `20261107_0025`; when `main` gains a revision, add a
merge revision on the branch with `down_revision = ("<Apple head>", "<main head>")`; never edit an
existing revision. `tests/test_apple_migration.py` and the snapshot script fail while there are
two heads. The lines the branch changes in shared files (`tests/test_migrations.py` table count,
`scripts/verify_compose.py` expected revision, the table lists of a few migration tests) are the
expected merge conflicts.

## Source precedence

When two sources report the same data, the interface shows one and keeps the other visible;
nothing is summed or averaged across sources, so nothing is counted twice. The rule is the
maintainer's decision of 7 November 2026, reviewed after a week:

| Category | What it holds | Default order (`.env`) |
|---|---|---|
| `activity` | workouts (`exercise_sessions`, Garmin activities, Fitbit exercises) and the heart rate, calories and distance measured *inside* a selected workout window | `SOURCE_PRECEDENCE_ACTIVITY=GARMIN,FITBIT,HEALTH_KIT` |
| `health` | everything else: daily steps, sleep, resting heart rate, HRV, SpO₂, continuous heart rate outside a workout, stress, body battery, weight and the other body measurements | `SOURCE_PRECEDENCE_HEALTH=FITBIT,GARMIN,HEALTH_KIT` |

A *source code* is the device platform a record came from, whatever delivered it: Google
records carry `dataSource.platform` (`FITBIT`, `HEALTH_KIT`, `HEALTH_CONNECT`; `GOOGLE` when it is
missing or a value was made from records of several platforms), Garmin rows are `GARMIN`, Apple
Health rows imported directly `HEALTH_KIT` (provider `APPLE`, [Apple Health](#apple-health-github-version)),
owner-entered rows `MANUAL`. A source the policy does not list ranks after every listed one
(`GOOGLE` right after them). **To reverse a rule, reorder its line** (`GARMIN,FITBIT` →
`FITBIT,GARMIN`) and restart; the stored rows do not change. Manual entries are never resolved
against device records: the owner typed them, they are shown as they are.

A *conflict* is the same metric from two sources over an overlapping time: the same calendar
day for a daily value, the same minute for a sample, overlapping start/end for a session
(`app/services/source_precedence.py`: `resolve`, `resolve_samples`, `resolve_sessions`). The
observations of a conflict are ordered by the category's list; the first is *selected*, the rest
are *alternatives*, and all of them are returned:

- `GET /api/dashboard` and Umay's `health_archive daily`: each day's `steps`, `sleep_seconds`,
  `resting_bpm`, `hrv_ms` and `spo2` hold the selected source's value (a day only Garmin
  measured shows Garmin's), and `sources[metric]` gives `source`, `label` and `alternatives`
  (`[{source, label, value}]`). The hourly heart chart takes, per minute, the health policy's
  source outside and the activity policy's source inside a Garmin workout window
  (`charts.sources.heart_rate`: `source`, `alternatives`, `activity_windows`,
  `minutes_by_source`). The response carries the policy as `precedence`.
- `GET /api/dashboard/workouts`: every row has `source` (the label: Fitbit, Garmin, Apple Sağlık,
  Manuel), `source_code`, `provider`, `priority` (`primary`/`secondary`), `group` (on a secondary
  row: the id of the primary it overlaps) and `alternatives` (on a primary row: the overlapping
  secondary sessions). A Fitbit auto-detected walk inside a Garmin run is the run's alternative;
  a reversed policy makes the Fitbit session primary.
- `GET /api/devices` and Umay's `devices`: every device has `source_code`, `provider` and
  `precedence` (`{"activity": primary|secondary|unranked, "health": …}`); the response carries
  `precedence` (the policy) and `garmin` (`ok` when the archive holds Garmin records, else `off`).
- `GET /api/garmin/daily`: `precedence` per metric and `policy`.

The interface shows the primary source as a text badge (never colour alone) and the secondary
sources in a collapsed row or "diğer kaynaklar"; the Cihazlarım entry of every device says in
which category it is primary. The policy is read from the configuration on every request; tests
pass a `Policy` explicitly.

## Local API

- `GET /health/live` (no database, no token; used by the container health check)
- `GET /health`
- `GET /login`, `POST /login` (exchange `APP_UI_TOKEN` for a session cookie)
- `GET /sync/status`
- `GET /sync/status/{data_type}`
- `POST /sync/{data_type}`
- `POST /sync/all`
- `POST /backfill/start`
- `GET /backfill/status`
- `POST /webhooks/google-health`
- `GET /auth/google/start` and `GET /auth/google/callback`
- `GET /google-health/capabilities` (metadata-only one-record probes)
- `GET /google-health/devices` (sanitized paired-device metadata)
- `GET /api/devices` (Cihazlarım: the archive's devices with their freshness status, see
  [Devices](#devices))
- `POST /webhooks/apple-health` (Health Auto Export receiver, its own token, off by default), `POST /api/apple/import` (export.zip/export.xml upload) and `GET /api/apple/status` ([Apple Health](#apple-health-github-version))
- `GET /api/garmin/status` and `GET /api/garmin/daily?from=YYYY-MM-DD&to=YYYY-MM-DD` ([Garmin Connect Personal API](#garmin-connect-personal-api)); `daily` says per metric whether Garmin is the primary or a secondary source ([Source precedence](#source-precedence))

Interactive OpenAPI documentation (`/docs`, `/redoc`, `/openapi.json`) is served only outside
production (`APP_ENV` other than `production`, e.g. with `docker-compose.dev.yml` or `make run`):
its page needs inline scripts and the jsDelivr CDN, an exception the login-cookie origin should not
carry in production. Every route except
`/health/live`, the login page, the OAuth callback, the Google webhook and the Apple receiver (which has its own token) requires the UI token.

## Database backup and restore

Daily backups are automated by [`deploy/systemd/healthos-backup.timer`](../deploy/systemd/healthos-backup.timer)
(03:30 local time, `Persistent=true` so a missed run happens after boot), which starts
[`deploy/systemd/healthos-backup.service`](../deploy/systemd/healthos-backup.service). The service
runs [`scripts/backup.sh`](../scripts/backup.sh) on the Docker host:

1. `docker compose --profile backup run --rm -T backup` streams a compressed custom-format
   `pg_dump` to stdout (the container refuses to write a dump to a terminal);
2. the stream is encrypted before it reaches the disk and written atomically as
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
[runbook](RUNBOOK.md#disk-space-for-backups)); `BACKUP_MIN_FREE_MB=0` disables the guard.

**Status file.** `last-backup-status.json` holds `timestamp` (UTC), `result` (`success` or
`failure`), `message`, `file`, `size_bytes`, `free_bytes`, `required_free_bytes`, `retention_days`
and `pruned_files`. It is replaced atomically on every run, so a stale `timestamp` means the timer
is not running. A failed run also starts
[`healthos-backup-failure.service`](../deploy/systemd/healthos-backup-failure.service) via
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
`make backup` (or `bash scripts/backup.sh`); the path of the new file is printed.

**Restore drill (non-destructive to the live database):**

```sh
make restore FILE=backups/health-20261020T033000Z.dump.age   # or: bash scripts/restore.sh FILE [db]
```

[`scripts/restore.sh`](../scripts/restore.sh) decrypts the file (age: `BACKUP_AGE_IDENTITY` or the
key file; gpg: the key file), drops and recreates a scratch database (default `health_restore`,
never `health`), restores into it with `pg_restore --exit-on-error`, and prints per-table row
counts next to the live database's counts plus the restored Alembic revision. Drop the scratch
database afterwards.

A dump contains private health data and Fernet-encrypted OAuth refresh tokens. **A restore is only
usable with the same `GOOGLE_TOKEN_ENCRYPTION_KEY`**; without it you must re-link Google. Store that
key and the backup key together in a safe place that is not the backup directory.

**Destructive command requiring explicit confirmation:** replacing the live database deletes its
current objects. Prove a current backup with the drill above and confirm the target before use.

```sh
docker compose stop app
age --decrypt --identity /path/to/identity backups/health-<stamp>.dump.age \
  | docker compose exec -T postgres pg_restore -U health -d health --clean --if-exists --no-owner
docker compose start app
docker compose exec app health-mirror verify
```

## Integrity verification

**Verification-only command:**

```sh
health-mirror verify
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

## Troubleshooting

- **`ACCOUNT_NOT_LINKED`**: the selected Google account is not linked to a Fitbit account.
- **No refresh token**: revoke the app's prior grant and repeat OAuth; the flow explicitly requests offline consent.
- **403 on health endpoints**: verify API enablement, test-user access, requested restricted scopes, and consent.
- **Backfill appears slow**: sleep/exercise pages are capped at 25 by Google; progress persists after every page.
- **Webhook subscriber creation fails**: confirm public HTTPS/TLS 1.2+, exact auth secret, both handshake results, and project **number**.
- **Webhook 401**: check the exact authorization header; do not replace Google's public keyset URL with an untrusted key.
- **Webhook 503 `Webhook verification temporarily unavailable`**: Google's public keyset could not be downloaded (`key_download_failed`, log `WEBHOOK_VERIFICATION_UNAVAILABLE`); this is not a signature verdict, Google retries the delivery. Check outbound network access to the keyset URL.
- **Repeated rows**: run `health-mirror verify`; database uniqueness is `(data_type, google_data_point_id)` for raw records.
- **Unknown new Google fields**: they are already retained in `raw_json`. Add or revise a parser later and reprocess locally without calling Google again.

## Analytics compatibility

Connect read-only analytics tools to the `analytics_*` views, such as
`analytics_heart_rate`, `analytics_sleep_sessions`, `analytics_energy_expenditure`, and
`analytics_scalar_metrics`. These views expose the normalized tables with what every reader
counts ([Derived tables](#derived-tables)): since `20261024_0013` (PostgreSQL) a row needs a live,
unquarantined raw row with its key, is not quarantined itself and is not a
[superseded duplicate](#record-identity). Before, `20261021_0012` left out only quarantined rows
and rows whose raw record was marked deleted or superseded, so a normalized row without a raw row,
or one whose raw row alone was quarantined, still counted in the views (and on the dashboard,
though not in its history). The underlying normalized tables keep every row for auditing, and the
raw JSON remains available for future Pandas/Jupyter, SQL, Grafana, n8n, or
local-agent pipelines. The archive itself has no AI layer; the optional Umay chat reads it only
on request.

### Quarantined rollup records

No raw health record is deleted during quarantine. Migration `20260818_0002` preserves every raw
and normalized row, adds `is_quarantined` and `quarantine_reason`, and creates an analytics-safe
view for every normalized table. Future UPSERTs deliberately do not overwrite quarantine metadata.

The live archive contains 20,165 rollups produced before request windows were fixed to canonical
UTC-minute boundaries. They are identified by all of these conditions:

```sql
data_type IN ('calories-in-heart-rate-zone', 'total-calories')
AND (
  extract(second FROM source_start_time) <> 0
  OR extract(microseconds FROM source_start_time) <> 0
)
```

Their stable reason code is `pre_fix_rollup_non_minute_boundary_v1`. The raw rows remain in
`raw_health_data`; corresponding rows remain in `energy_expenditure` or `scalar_metrics` for audit
purposes. Use the `analytics_*` views for future analysis. If a base normalized table must be queried
directly, add `WHERE is_quarantined = false`.

## Non-goals

- No medical diagnosis or health advice; AI analysis exists only in the optional, on-request Umay chat.
- No writes, patches, or deletes against Google Health.
- No legacy Fitbit Web API compatibility layer.
- No mandatory hosted service, paid API, or public database exposure.
