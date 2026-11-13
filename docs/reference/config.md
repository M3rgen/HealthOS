# HealthOS reference — Configuration

Part of the [technical reference](../REFERENCE.md).

## Configuration

Copy `.env.example` to `.env`; never commit `.env`.

| Variable | Purpose |
|---|---|
| `APP_ENV`, `APP_HOST` | `production` (or `prod`) turns on the startup guard and hides `/docs`; `development` and `test` are the other accepted values (any case), and anything else, including surrounding spaces or an empty value, is a startup error; `docker-compose.yml` forces `production` and binds `0.0.0.0` inside the container, published on `127.0.0.1` only. Default `development`, `127.0.0.1` |
| `APP_PORT` | Port Uvicorn binds (default 8000). Under Compose the container always listens on 8000; `APP_PORT` sets only the host port it is published on |
| `LOG_LEVEL` | Logging level of the app's JSON logs (default `INFO`) |
| `POSTGRES_PASSWORD` | Password of the Compose database owner; `.env` only, read by Compose. Compose builds the owner `DATABASE_URL` from it for `migrate`, `provision-app-role` and `backup`; the app only receives it when `POSTGRES_APP_*` are unset |
| `POSTGRES_APP_USER`, `POSTGRES_APP_PASSWORD` | Non-superuser login the Compose app uses at runtime. The one-shot `provision-app-role` service creates or refreshes the role and its grants after every migration, and the schema stays owned by the migration role. Set both: the user is a plain lower-case identifier that names no privileged or owner role, and the password is 24–512 URL-safe characters. `.env.example` sets `healthos_app`. With both removed, a database that holds records keeps connecting as the owner, which production refuses when it is a superuser (see `HEALTHOS_ALLOW_SUPERUSER_UNTIL`), and a new, empty one stops at `provision-app-role` |
| `HEALTHOS_ALLOW_SUPERUSER_UNTIL` | An upgrade aid, not a setting to keep: a date (`YYYY-MM-DD`) at most 30 days ahead until which production still starts while the database login is a superuser, with a warning and the `/health` reason `database_role_superuser_grace`. Unset (the default), a past date or a later one: production refuses a superuser login and names the `POSTGRES_APP_*` step |
| `POSTGRES_ALLOW_OWNER_RUNTIME` | `1` lets a new, empty database start without `POSTGRES_APP_*`, running the app as the database owner. Read only by `provision-app-role`; leave unset unless you decided that on purpose |
| `APP_UI_TOKEN` | Single-user access key for the UI and API (32–512 printable characters; production also applies the [strength check](#secret-strength)). Required in production; empty disables login only outside production with loopback `APP_HOST`. Tokenless private requests require a loopback client address; a loopback listener alone is not enough. Sent as `Authorization: Bearer …` or exchanged for a cookie at `/login` |
| `APP_PUBLIC_URL` | UI URL; default empty. Empty or `healthos.localhost`: page navigations on plain `localhost` move to `healthos.localhost`; any other address (for example `http://localhost:8000`) keeps the interface there. HTTPS forces Secure session cookies. Leave empty for local HTTP; never use the Cloudflare webhook tunnel URL (see [UI auth](../UI_AUTH.md)) |
| `APP_TRUST_PROXY_HEADERS` | Default `false`; trust `X-Forwarded-Proto` only behind a controlled proxy that replaces it. Run Uvicorn with `--no-proxy-headers` so the application owns this decision |
| `DATABASE_URL` | Async SQLAlchemy PostgreSQL URL |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth web client; leave blank until integration |
| `GOOGLE_REDIRECT_URI` | Must exactly match the authorized redirect URI; default `http://localhost:8000/auth/google/callback`. A start on another local host (`healthos.localhost`) moves to this origin with a single-use ticket, so no UI login is needed there |
| `GOOGLE_REFRESH_TOKEN` | Optional environment-only refresh token alternative |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | Fernet-format root key; HKDF derives separate refresh-token encryption and OAuth-state HMAC keys. With `google` in production a malformed key (or one in `GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD`) stops startup instead of the first OAuth step |
| `GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD` | Optional comma-separated previous root keys (default: empty); validated, but not used to read tokens. Changing the current root requires fresh Google consent through "Connect Google"; retired roots cannot restore access. See [Rotate secrets](../RUNBOOK.md#rotate-secrets) |
| `GOOGLE_API_MIN_REQUEST_INTERVAL_SECONDS` | Shared request pacing interval; defaults to `0.25` (4 requests/second) |
| `GOOGLE_CLOUD_PROJECT_NUMBER` | Numeric Google Cloud project number used by subscriber APIs; not the project ID |
| `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` | Public self-hosted HTTPS receiver URL ending in `/webhooks/google-health` |
| `GOOGLE_HEALTH_WEBHOOK_SUBSCRIBER_ID` | Stable 4–36 character subscriber ID; defaults to `personal-health-mirror` |
| `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` | Exact secret header value configured for the subscriber, e.g. `Bearer …`. With `google` in production it must pass the [strength check](#secret-strength) |
| `GOOGLE_HEALTH_WEBHOOK_PUBLIC_KEY_URL` | Google's permanent public signing keyset URL |
| `GOOGLE_HEALTH_BASE_URL` | Google Health API root, default `https://health.googleapis.com/v4`. Production refuses anything but `https://` on a `*.googleapis.com` host (no user, port or query), because bearer access tokens go there |
| `GOOGLE_HEALTH_WEBHOOK_MAX_BODY_BYTES` | Largest accepted webhook body, verification payloads included; default 2 MiB (1 KiB to 16 MiB) |
| `WEBHOOK_REQUESTS_PER_MINUTE` | Capacity of each webhook rate budget, default 120; requests with and without the configured authorization count separately |
| `MANUAL_WRITE_MAX_BODY_BYTES`, `MANUAL_WRITE_REQUESTS_PER_MINUTE` | Limits on manual measurement, workout and flag POSTs and on job starts (`/sync/*`, `/backfill/start`): 4096 bytes per body (1024 to 65536) and 60 requests per minute application-wide |
| `CLOUDFLARED_CREDENTIALS_FILE`, `CLOUDFLARE_WEBHOOK_HOSTNAME` | Inputs of the Cloudflare Tunnel templates in `deploy/cloudflare/` ([Cloudflare Tunnel](../CLOUDFLARE_TUNNEL.md)); the application does not read them |
| `WEBHOOK_HOST` | Public DNS hostname used by the optional self-hosted Caddy TLS profile |
| `BACKUP_HOST_DIR`, `BACKUP_RETENTION_DAYS`, `BACKUP_MIN_FREE_MB` | Host directory for encrypted dumps; opt-in retention in days (default: empty, keep forever; only a positive whole number deletes, anything else keeps everything; set in the environment, even empty, it overrides `.env`); free-space guard in MiB (default: twice the newest dump, at least 1 GiB, or 5 GiB before the first dump); read by `scripts/backup.sh` |
| `BACKUP_STATUS_FILE`, `BACKUP_STATUS_HOST_DIR` | Path of the backup status metadata inside the app (Compose sets it to `/run/healthos-backup/status.json`) and its host directory (default `./backup-status`). The older `WINDOWS_BACKUP_HOST_DIR` and `LINUX_BACKUP_HOST_DIR` are ignored |
| `POSTGRES_MEM_LIMIT`, `APP_MEM_LIMIT`, `MIGRATE_MEM_LIMIT`, `BACKUP_MEM_LIMIT`, `POSTGRES_PIDS_LIMIT`, `APP_PIDS_LIMIT`, `MIGRATE_PIDS_LIMIT` | Compose container limits (defaults `4g`, `2g`, `2g`, `1g`, `256`, `256`, `128`); the `provision-app-role` and `caddy` containers are fixed at `256m`, `64` and the `backup` container's PID limit at `32` |
| `BACKFILL_START_DATE`, `BACKFILL_WINDOW_DAYS` | Earliest attempted history and checkpoint window size |
| `GOOGLE_API_PAGE_SIZE`, `GOOGLE_API_MAX_RETRIES` | Page and exponential retry policy |
| `SYNC_CONCURRENCY` | Data types synchronized at once, default 2 (1 to 10); also sizes the database connection pool (`3 × SYNC_CONCURRENCY + 3` connections, no overflow: 9 at the default) |
| `EXTRA_DATA_TYPES` | Comma-separated evolving v4 data types; defaults to interval filtering |
| `ENABLED_DATA_TYPES` | Optional comma-separated allowlist; blank enables the complete built-in registry |
| `RECONCILIATION_DAILY_HOUR_UTC` | Integer, default `2` (0 to 23); UTC hour at which the daily reconciliation starts |
| `RECONCILIATION_WEEKLY_DAY` | String, default `sun`; APScheduler day-of-week expression for weekly reconciliation (`mon` to `sun`, or `0` to `6`, Monday = 0). Checked when scheduling; no Settings field validator |
| `RECONCILIATION_WEEKLY_HOUR_UTC` | Integer, default `3` (0 to 23); UTC hour at which the weekly reconciliation starts |
| `RECONCILIATION_DAILY_DAYS` | Integer, default `7` (at least 1); lookback in days for the daily reconciliation |
| `RECONCILIATION_WEEKLY_DAYS` | Integer, default `90` (at least 1); lookback in days for the weekly reconciliation |
| `IDENTITY_RECONCILE_ENABLED`, `IDENTITY_RECONCILE_HOUR_UTC`, `IDENTITY_RECONCILE_MINUTE` | Nightly identity reconciler (default on; starts when the scheduled reconciliations have ended, at the hour and minute, default 02:30 UTC, only when `RECONCILIATION_ENABLED` is `false`; see [Reconciliation](reconciliation.md#reconciliation)) |
| `IDENTITY_RECONCILE_UNTIL_HOUR_UTC`, `IDENTITY_RECONCILE_UNTIL_MINUTE` | End of the nightly window up to which the identity reconciler retries a data type whose lock stays busy (default 03:55 UTC) |
| `IDENTITY_RECONCILE_LOCK_WAIT_MINUTES` | How long a run started after that window end, and `identity-backfill` by hand, retries a busy data type (default 60; `0`: 8 attempts only) |
| `WATCHDOG_ENABLED`, `WATCHDOG_INTERVAL_SECONDS` | Enable local pipeline checks; defaults to every 15 minutes |
| `PIPELINE_STALE_THRESHOLD_MINUTES` | Age at which frequently updated sync streams are stale; defaults to 120 minutes |
| `PIPELINE_FRESHNESS_REFRESH_SECONDS` | How often `/health` recomputes its data-freshness summary, default 3600 (60 to 86400); it serves the cached result in between |
| `STARTUP_GAP_THRESHOLD_MINUTES` | Minimum persisted sync/write gap that permits startup recovery; defaults to 120 minutes |
| `AUTOMATIC_RECOVERY_ENABLED`, `AUTOMATIC_RECOVERY_MAX_DAYS` | Enable bounded reconciliation recovery and cap its lookback; defaults to 7 days |
| `RECOVERY_COOLDOWN_MINUTES` | Minimum time between automatic recovery attempts; defaults to 360 minutes |
| `DEVICES_CONNECTED_HOURS`, `DEVICES_DELAYED_DAYS` | **Cihazlarım** status thresholds: a device whose newest record is at most `DEVICES_CONNECTED_HOURS` old (default 48) is `bagli`, older than `DEVICES_DELAYED_DAYS` (default 7) `kopuk`, `gecikmeli` in between |
| `HEALTHOS_SOURCES` | Which sources run: comma-separated `google`, `garmin`, `apple` (trimmed, duplicates ignored), or `none` alone. `.env.example` sets `none`; a `.env` with the line removed behaves as `google`, plus Garmin with the older `GARMIN_ENABLED=true`. What each selection does, and what is not in this tree, is on [Sources](sources.md#sources). Production always requires a non-default database password and `APP_UI_TOKEN`; Google secrets and an HTTPS webhook URI only with `google`, a readable `GARMIN_API_KEY_FILE` only with `garmin`. The active list is `active_sources` in `/health` and `/api/status` |
| `GARMIN_API_KEY_FILE`, `GARMIN_PULL_INTERVAL_MINUTES`, `GARMIN_LOOKBACK_DAYS` | Read-only pull from the Garmin Connect Personal API when `HEALTHOS_SOURCES` includes `garmin` (the key is read from a file, default `~/.config/garmin/connect-personal-api-key`; every 120 minutes outside 02:00–03:59 UTC; the last 3 days; see [Garmin Connect Personal API](garmin.md#garmin-connect-personal-api)) |
| `GARMIN_ENABLED` | Older switch, read only when `HEALTHOS_SOURCES` is absent (default `false`); with `HEALTHOS_SOURCES` present it is ignored |
| `GARMIN_DEVICE_NAME` | What **Cihazlarım** calls the Garmin source (default `Garmin`; the Personal API exposes no device model) |
| `APPLE_RECEIVER_ENABLED`, `APPLE_RECEIVER_TOKEN_FILE`, `APPLE_RECEIVER_ENDPOINT_URI` | Health Auto Export receiver (GitHub version; off by default; bearer token read from a file, default `~/.config/healthos/apple-receiver-token`; the optional https endpoint URI admits its host for `POST /webhooks/apple-health` only; see [Apple Health](apple.md#apple-health-github-version)) |
| `APPLE_RECEIVER_MAX_BODY_BYTES`, `APPLE_RECEIVER_REQUESTS_PER_MINUTE`, `APPLE_IMPORT_MAX_UPLOAD_BYTES`, `APPLE_IMPORT_MAX_UNCOMPRESSED` | Receiver body limit (default 32 MiB) and request budget (default 30 per minute, authorized and unauthorized apart); largest `export.zip` upload (default 8 GiB); most bytes a zip may decompress to (default 16 GiB; a 100:1 ratio guard also holds) |
| `SOURCE_PRECEDENCE_ACTIVITY`, `SOURCE_PRECEDENCE_HEALTH` | Which source is shown first when several report the same data: sport activities `GARMIN,FITBIT,HEALTH_KIT`, every other health metric `FITBIT,GARMIN,HEALTH_KIT` by default; the others stay visible as alternatives, nothing is summed; reverse a line to reverse the rule (see [Source precedence](source-precedence.md#source-precedence)) |
| `CODEX_BRIDGE_BIND_HOST`, `CODEX_BRIDGE_URL`, `CODEX_BRIDGE_TOKEN` | Optional Umay bridge address and token; manual launcher preserves configured URLs, service reads existing credentials. The launcher accepts as `CODEX_BRIDGE_BIND_HOST` only loopback, `docker0` and Compose `br-*` bridge addresses, never `0.0.0.0`. In production the URL must be `ws`/`wss` on loopback or the Docker host gateway, and a configured token must pass the [strength check](#secret-strength) (empty keeps Umay off). The URL is dialled as `wss://` to the bridge's TLS front ([details](../ARCHITECTURE.md#local-codex-chat-umay-bridge)) |
| `CODEX_BRIDGE_ALLOW_NON_LOCAL_BIND` | Read by the bridge launcher only (process environment or `.env`), default off. `1` lets `CODEX_BRIDGE_BIND_HOST` name an address that is neither loopback nor a Docker bridge, with a warning at every start; restrict port 8766 with the firewall. `0.0.0.0` stays refused |
| `HEALTHOS_ALLOW_LEGACY_SECRETS` | Default `false`. Keeps a secret that passes only the previous [strength](#secret-strength) rule, warning at every start, so an upgrade cannot stop an installation before its secrets are rotated. A secret below that rule is never accepted |

### Secret strength

In production the app refuses to start, naming the setting, when `APP_UI_TOKEN`, a configured
`CODEX_BRIDGE_TOKEN`, (with `google`) `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` or the password in
`DATABASE_URL` (under Compose `POSTGRES_APP_PASSWORD`, or `POSTGRES_PASSWORD` while the app or
`migrate` connects as the owner) is weaker than a generated secret (`app/core/secret_strength.py`). Each must be at least 32 characters, not counting a
`Bearer ` or `Basic ` prefix; have at least 2.5 bits of entropy per character; not be one unit
repeated (`password` four times) or a word repeated behind changing numbers or separators; not be
mostly consecutive characters (`abcdef…`, `87654…`); and not contain a common password (`password`,
`passw0rd`, `qwerty`, `letmein`, `iloveyou`, `changeme`, in any case). `APP_UI_TOKEN` must also be
32–512 printable ASCII characters without spaces. The values are never printed.
`secrets.token_urlsafe(32)` and `secrets.token_hex(32)` pass, `token_hex(16)` (32 characters) is
the shortest generated value that can, and the 16 characters of `token_hex(8)` do not. A long password chosen by a person can still pass, so
generate the value.

The previous rule asked for 16 characters and had no password list. A secret that passes only that
rule stops startup with a message naming it and the option below; with
`HEALTHOS_ALLOW_LEGACY_SECRETS=true` it is kept, with a warning at every start, until you replace it.
A secret that fails the previous rule too is never accepted. The Umay bridge launcher applies the
same rule to `CODEX_BRIDGE_TOKEN` and reads the same option.

Generate an encryption key locally and put it only in `.env`.

**Cross-platform command (prints a new secret; store it immediately):**

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```
