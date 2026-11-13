# HealthOS reference — Local API

Part of the [technical reference](../REFERENCE.md).

## Local API

With Google and Garmin selected, the app registers 33 OpenAPI paths. The paths and their methods,
plus the WebSocket, are listed below, from the routers in `app/api/` as `app/main.py` includes them.
Paths take query parameters as shown; JSON bodies are described where they matter.

**Always present**

- `GET /health/live` (no database, no token; used by the container health check)
- `GET /health` and `GET /api/status` (same response; `active_sources` lists the selected sources,
  `last_backup_success` the last proven backup, see below)
- `GET /health/backup` (result of the last backup attempts)
- `GET /login`, `POST /login` (exchange `APP_UI_TOKEN` for a session cookie)
- `POST /logout` (same-origin form; revokes the session cookie)
- `GET /api/dashboard?days=7&end_date=YYYY-MM-DD&timezone=<zone>` (the overview: daily values,
  profile and cards; `days` 1–31, no future `end_date`)
- `GET /api/dashboard/export.csv` (same parameters; the overview's days as CSV with a BOM,
  `no-store`)
- `GET /api/dashboard/history?metric=steps|sleep|heart&page=1&timezone=<zone>` (thirty civil days
  per page, newest first, through the whole archive)
- `GET /api/dashboard/measurements` and `POST /api/dashboard/measurements` (manual entries; body
  `metric` (`height`, `weight`, `body_fat`, `muscle`), `value`, `measured_at`)
- `POST /api/dashboard/measurements/{measurement_id}/flag` (UUID id; optional JSON body:
  `flag` defaults to `"test"` and accepts only that value, optional `reason`; returns
  `id`, `flags`, `changed`: `201` for a new event, `200` if already flagged, `404` if absent).
- `POST /api/dashboard/measurements/{measurement_id}/unflag` (same body and response; `201` for
  a new event, `200` if already unflagged, `404` if absent). Both require a login session or bearer
  token and `X-HealthOS-Request: 1`; decisions append events and leave the entry unchanged
  ([Architecture](../ARCHITECTURE.md)).
- `GET /api/dashboard/workouts?month=YYYY-MM-DD&timezone=<zone>` and
  `POST /api/dashboard/workouts` (manual workouts; body `exercise_type`, `start_time`,
  `end_time`, optional `calories_kcal`, `distance_meters`)
- `GET /api/settings/appearance`, `PUT /api/settings/appearance` (the shared look; last write
  wins, body at most 4 KiB)
- `GET /api/settings/display`, `PUT /api/settings/display` (shared display preferences; same
  limit, `409` when the stored row was written by a newer version)
- `GET /api/devices` (**Cihazlarım**: the archive's devices with their freshness status, see
  [Devices](identity-and-versions.md#devices))
- `POST /webhooks/apple-health` (Health Auto Export receiver, its own token, off by default), `POST /api/apple/import` (export.zip/export.xml upload) and `GET /api/apple/status` ([Apple Health](apple.md#apple-health-github-version))
- `GET /api/demo`, `POST /api/demo?timezone=<zone>`, `DELETE /api/demo` (no request body;
  `GET` returns `loaded`, `rows`, `first_day`, `last_day`; `POST` loads synthetic data into an
  archive without device data and adds an `added` count map; `DELETE` removes only synthetic
  data and adds a `removed` count map. Success is `200`; refused writes return `409`.
  All require a login session or bearer token; writes also need `X-HealthOS-Request: 1`.)
- `POST /api/imports/garmin-fit` (raw FIT body, at most 8 MiB; `application/octet-stream` or
  `application/vnd.ant.fit`, no multipart or ZIP; returns `200` with `sha256`, `records`,
  `inserted`, `updated`, `unchanged`, `no-store`; `413` oversized, `415` unsupported media type,
  `422` invalid FIT. Requires a login session or bearer token and `X-HealthOS-Request: 1`;
  works without Garmin selected or an API key.)
- `WS /api/codex/chat` (Umay's chat; same-origin WebSocket with the login cookie or bearer token,
  [bridge](../ARCHITECTURE.md#local-codex-chat-umay-bridge))

**Only when Google is selected**

- `GET /sync/status`
- `GET /sync/status/{data_type}`
- `POST /sync/{data_type}`
- `POST /sync/all`
- `POST /backfill/start` (optional `?data_type=`; while a backfill of the same selection runs, the
  request joins it: the running job is left alone, the answer is the same `202`, and only a request
  after it finished starts another. "All types" and a single type are separate selections)
- `GET /backfill/status`
- `GET /api/sync/reconciliation?offset=0&limit=20` (recent reconciliation runs)
- `GET /api/sync/coverage?start_date=…&end_date=…&timezone=<zone>` (optional `data_type`,
  `offset`, `limit` 1–4; at most 31 days; which days were retrieved and why a day has no value)
- `GET /api/webhooks/status` (receiver readiness and counters, `no-store`)
- `POST /webhooks/google-health`
- `GET /auth/google/start` and `GET /auth/google/callback`
- `GET /auth/google/handoff?ticket=…` (no body, no UI session or `X-HealthOS-Request` header;
  requires the configured callback origin and a single-use ticket issued by an authenticated
  start, valid for 120 seconds. Sets the OAuth binding cookie and redirects to Google with
  `307`, `no-store`; `400` for an invalid, expired or spent ticket or wrong origin;
  `503` when OAuth credentials or state storage are unavailable.)
- `GET /google-health/capabilities` (metadata-only one-record probes)
- `GET /google-health/devices` (sanitized paired-device metadata)

**Only when Garmin is selected**

- `GET /api/garmin/status` and `GET /api/garmin/daily?from=YYYY-MM-DD&to=YYYY-MM-DD` ([Garmin Connect Personal API](garmin.md#garmin-connect-personal-api)); `daily` says per metric whether Garmin is the primary or a secondary source ([Source precedence](source-precedence.md#source-precedence))

Which sources are selected is described in [Sources](sources.md#sources). The built interface is
served at `/` with its files under `/assets` (and `/models` for the 3D body), outside the API.

The job-start and write routes (`POST /sync/*`, `POST /backfill/start`, the `POST` dashboard routes
and FIT import, `POST`/`DELETE /api/demo`, and the `PUT` settings routes) need the header
`X-HealthOS-Request: 1` and refuse cross-site requests; the `POST` ones and `DELETE /api/demo`
share the manual-write budget (`429` when it is spent, see
[configuration](config.md#configuration)). A Google account change is not an API route: the OAuth
callback refuses a different account with `409`, and `healthos google-rebind
--confirm-account-change` changes it from the command line ([CLI](cli.md)).

`/health` also reports the backups among its `reasons`, without changing `status`, because the
container health check and the interface read `status` as "the service works":
`backup_missing` (no status file yet, for example before the first timer run),
`backup_failed` (the last attempt failed) and `backup_status_unreadable` (the file cannot be read or
parsed). They appear only when a status file is configured, as Compose does.

Interactive OpenAPI documentation (`/docs`, `/redoc`, `/openapi.json`) is served only outside
production (`APP_ENV` other than `production`, e.g. with `docker-compose.dev.yml` or `make run`):
its page needs inline scripts and the jsDelivr CDN, an exception the login-cookie origin should not
carry in production. Every route except
`/health/live`, the login page and `POST /login`, `POST /logout`, the OAuth callback and handoff,
the Google webhook and the Apple receiver (which has its own token) requires a login session or
bearer UI token when `APP_UI_TOKEN` is configured.
