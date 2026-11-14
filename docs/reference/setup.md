# HealthOS reference — Setup

Part of the [technical reference](../REFERENCE.md).

## Requirements

- Docker Engine with Docker Compose, or Python 3.12 or 3.13 and PostgreSQL 15+
- `uv` 0.13.6 for deterministic native dependency installation
- A Google Cloud project with Google Health API access, only for the Google source (not needed for Garmin, development or tests)
- For webhooks: a domain/public HTTPS URL that routes to `/webhooks/google-health`

By setup:

| Setup | Requirements |
| --- | --- |
| **Recommended: Docker** | Git, Docker Engine or Docker Desktop, Docker Compose v2, and a modern browser with WebGL. Docker builds the frontend, so the host needs no Node.js. |
| **Backups** | Python 3.11+ on the host for `make backup` and the backup timer. |
| **Native development** | `uv` 0.13.6, PostgreSQL, and Node.js 24 for frontend builds ([Development](../DEVELOPMENT.md)). The shipped Compose stack uses PostgreSQL 17. |
| **Garmin** | A Garmin Connect account and a Personal API key ([Garmin](garmin.md#garmin-connect-personal-api)). |
| **Google synchronization** | An eligible Google Cloud project, Google Health API access, an OAuth web client, and a linked Google Health account. |
| **Server deployment** | A Linux server (Debian is documented) with Docker Engine; see [Deployment](../DEPLOYMENT.md). |
| **Optional webhooks** | A public HTTPS endpoint that forwards only `/webhooks/google-health`; the documented setup uses a Cloudflare Tunnel. |
| **Optional Umay** | Codex CLI 0.161.0 exactly (the bridge refuses other releases) and an authenticated ChatGPT account on the Docker host; the bridge launcher supports Linux, macOS and Windows. |

Internet access is needed to build images and to reach Garmin or Google; Umay also needs access to
OpenAI. Storage depends on how much history you archive.

## Docker setup

Copy `.env.example` to the ignored `.env` and set a strong `POSTGRES_PASSWORD`,
`POSTGRES_APP_PASSWORD` (24–512 URL-safe characters) and an `APP_UI_TOKEN` (32+ characters).
Compose refuses to start without `POSTGRES_PASSWORD` or `APP_UI_TOKEN`, and the template's
`POSTGRES_APP_USER` needs its password.

The base Compose file sets `APP_ENV=production` for the app service (overriding `.env`), so the
application refuses to start when a value required for the sources in `HEALTHOS_SOURCES` is missing
or a placeholder: always `APP_UI_TOKEN` (at least 32 characters, and passing the [strength check](config.md#secret-strength)) and a non-default
database password, plus a loopback or Docker-gateway `CODEX_BRIDGE_URL` and a strong `CODEX_BRIDGE_TOKEN` when one is set; with `google` the
Google OAuth client, `GOOGLE_TOKEN_ENCRYPTION_KEY`, an HTTPS `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI`
and a `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` that passes the same check; with `garmin` a readable `GARMIN_API_KEY_FILE`. The full
production setup is in [DEPLOYMENT.md](../DEPLOYMENT.md).

If `.env` does not exist, run `make env` to generate its three secrets with mode 600 (Python
3.9+); it refuses to replace an existing file. By hand, `cp .env.example .env && chmod 600 .env`,
then set:

- `POSTGRES_PASSWORD`: a long random value. A hexadecimal password avoids URL-escaping issues in
  the Compose database URL.
- `POSTGRES_APP_PASSWORD`: the password of the non-superuser role the app connects with
  (`POSTGRES_APP_USER`, `healthos_app` in the template). The schema stays owned by the migration
  role, so the app cannot disable the append-only triggers. Hexadecimal too; without it
  `provision-app-role` stops and the app does not start.
- `APP_UI_TOKEN`: the access key for the dashboard and API, 32 to 512 printable characters without
  spaces, and not guessable ([what production refuses](config.md#secret-strength)). You type it
  once on the login page; scripts send it as `Authorization: Bearer <token>`.

```sh
python3 -c "import secrets; print(secrets.token_hex(32))"        # POSTGRES_PASSWORD, POSTGRES_APP_PASSWORD (run once each)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"    # APP_UI_TOKEN
```

Store the results only in `.env`, which Git ignores; `.env.example` has blank secret fields and
sets `HEALTHOS_SOURCES=none`, so nothing else is needed to start. Create `backup-status`
(`install -d -m 755 backup-status`) before the first `docker compose up`, so the host user owns it
instead of Docker creating it as root.

After `docker compose up -d --build`, open `http://healthos.localhost:8000` and enter
`APP_UI_TOKEN`. The session lasts 30 days there or over HTTPS; plain `localhost` and `127.0.0.1`
redirect there before showing the login form ([why](../UI_AUTH.md#session-lifetime)). The first
build downloads dependencies and takes a few minutes; the one-shot `migrate` and
`provision-app-role` services apply migrations and create the app's role before the app starts.
You get an empty archive, the dashboard and manual entries, in production mode. After logging in,
`http://healthos.localhost:8000/health` shows pipeline status. Interactive API documentation
(`/docs`) is off in production.

For development, add the override, which sets `APP_ENV=development` and also publishes PostgreSQL
on `127.0.0.1:5432`. First create `.env` and fill in the three values as described in
[README step 1](../../README.md#1-clone-and-configure):

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health/live
```

The one-shot `migrate` service runs `alembic upgrade head` before the app starts. Compose marks the app healthy once
`/health/live` answers; `/health` additionally queries PostgreSQL and needs the UI token
(`printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - http://127.0.0.1:8000/health`).

## Native development

**Cross-platform command:**

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic upgrade head
uv run --frozen --extra dev uvicorn app.main:app --reload --host 127.0.0.1 --no-access-log --no-proxy-headers
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

The script uses a uniquely named Compose project prefixed `healthos-smoke-`, a temporary env
file derived from `.env.example` with synthetic non-placeholder values (the app runs with
`APP_ENV=production`), a temporary password, UI bearer token, backup directory and throwaway backup key, and a free
host port it picks itself. It builds the production image, waits for PostgreSQL and FastAPI, exercises `/health`
and the installed CLI, takes an encrypted backup with `scripts/backup.sh` (age if installed,
otherwise gpg), restores it with `scripts/restore.sh` into a second database and checks the row
counts, verifies the PostgreSQL Alembic downgrade/upgrade
lifecycle, checks the hardened container and private bindings, then removes only those isolated
resources. It does not read or modify `.env`, the normal `postgres_data` volume, or Google
credentials. Keep live-account verification evidence private; it can contain personal health data.

Deployment, routine operations, image maintenance, and Cloudflare setup are split into the [deployment](../DEPLOYMENT.md), [runbook](../RUNBOOK.md), [image update](../IMAGE_UPDATES.md), and [Cloudflare Tunnel](../CLOUDFLARE_TUNNEL.md) guides. Notes for Windows hosts and the earlier
Windows-to-Linux move are in [legacy/WINDOWS.md](../legacy/WINDOWS.md).

## OAuth and Google Health setup

Do this only after local tests pass. Exact Google Console labels can evolve; follow the current [Google Health Cloud and OAuth setup](https://developers.google.com/health/setup) and [Google OAuth web-server documentation](https://developers.google.com/identity/protocols/oauth2/web-server).

HealthOS uses the authorization-code flow with PKCE (`S256`); the code verifier stays in memory with the pending `state`.

1. Confirm Google Health API access is available to your project, then create or select the project and enable the API. As of October 3, 2026, Google states that new projects are not being onboarded; see the [official setup guide](https://developers.google.com/health/setup).
2. In **Google Auth Platform > Audience**, initially leave the personal integration in Testing and add the Google account used by Fitbit as a test user.
3. In **Google Auth Platform > Data Access**, add the read-only Google Health scopes required by the enabled data types. With the default registry these are `googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`, `googlehealth.nutrition.readonly`, `googlehealth.ecg.readonly`, `googlehealth.irn.readonly`, and `googlehealth.settings.readonly`. Set `ENABLED_DATA_TYPES` first if you want a smaller consent surface.
4. Create an OAuth 2.0 **Web application** client.
5. Add the exact authorized redirect URI, initially `http://localhost:8000/auth/google/callback`. No JavaScript origin is needed for this server-side flow.
6. Copy `.env.example` to the untracked `.env`. Put only `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and a newly generated `GOOGLE_TOKEN_ENCRYPTION_KEY` there; never send these values in chat.
7. Restart the app and visit `http://localhost:8000/auth/google/start` while signed into the same Google account used by the Fitbit app. Perform the capability probe and small `steps` sync below while still in Testing.
8. Before relying on unattended synchronization, move the OAuth audience to **In production** under Google's applicable personal-use exception, or complete any verification Google requires for your configuration. Repeat the OAuth route afterward so the mirror stores a production-mode refresh token. Google documents that Testing-mode refresh tokens expire after seven days.

The app requests only the read-only scopes represented by its configured data types, with `access_type=offline`. The callback encrypts the refresh token, calls `users/me/identity`, and stores the stable Health user mapping. If Google later rotates the refresh token during an access-token refresh, the replacement is encrypted and committed before it is used on the next restart. Do not paste credentials into source files, issues, logs, or chat.

The callback accepts only the Google account already stored: another account is refused with `409`
and nothing changes. To switch accounts on purpose, run `healthos google-rebind
--confirm-account-change`. It asks for the new account's refresh token without echoing it,
verifies the account through `users/me/identity`, and appends a new credential version; when the
account differs it also appends a `google_account_rebind_events` row. Earlier credentials and the
archive stay as they are.

Before writing any health records, run the metadata-only capability probe. It requests at most one recent point per configured type and reports accessibility, timestamps, and top-level field names without returning or storing health values.

**Verification-only command:**

```bash
healthos probe --days 30
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - 'http://127.0.0.1:8000/google-health/capabilities?days=30'
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - http://127.0.0.1:8000/google-health/devices
```

Run `healthos` inside the container (`docker compose exec app healthos …`) or in the
native environment. Every HTTP route except `/health/live`, `/login`, the OAuth callback and the
webhook needs the UI token: `Authorization: Bearer <APP_UI_TOKEN>`, or the login cookie in a
browser. The cookie names a random session issued at login and held in memory for at most 30 days (24 hours on plain `localhost`, see [UI auth](../UI_AUTH.md#session-lifetime));
`POST /logout` (same-origin form, the Sistem panel's **Çıkış yap**) revokes it, and an app restart
ends all sessions. See [DEPLOYMENT.md](../DEPLOYMENT.md) for opening the UI as `healthos.localhost`.

Use this to confirm the account is linked, Fitbit data is visible, and the configured scopes cover the intended types. Then verify a small persisted sync before starting the full backup.

**Writes retrieved records to the configured database:**

```bash
healthos sync steps
healthos status
```

## Google Health with Docker

The README's quickstart path for Google, in full. Only with a project that already has API access.
Add `google` to `HEALTHOS_SOURCES` once the values below are in `.env`: in production the app
refuses to start without them.

1. Follow Google's [Cloud and OAuth setup guide](https://developers.google.com/health/setup). Link
   your account in the Google Health mobile app first.
2. Create a **Web application** OAuth client and register the default redirect URI in Google Cloud
   Console before connecting: `http://healthos.localhost:8000/auth/google/callback` (use your
   installation's port). It must exactly match `GOOGLE_REDIRECT_URI`, or Google returns
   `redirect_uri_mismatch`. Existing installations may keep an explicit
   `http://localhost:8000/auth/google/callback` in `.env`; the single-use handoff from
   `healthos.localhost` still avoids a second UI login on `localhost`. If the variable is absent,
   register the new default before upgrading or explicitly keep the old value.
3. Configure the required **read-only** scopes. Optionally narrow the mirrored types with
   `ENABLED_DATA_TYPES`; see the [configuration reference](config.md#configuration).
4. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`. Generate a Fernet key with the
   built image and save it as `GOOGLE_TOKEN_ENCRYPTION_KEY`:

   ```sh
   docker compose run --rm --no-deps app python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

5. Set `HEALTHOS_SOURCES=google` (or `garmin,google`). Production also requires the webhook values
   described below (a `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` that passes the
   [strength check](config.md#secret-strength)), so until they are set, run with the development
   override, then visit `http://healthos.localhost:8000/auth/google/start` and complete consent:

   ```sh
   compose_files="$(docker compose config --environment | sed -n 's/^COMPOSE_FILE=//p')"; COMPOSE_FILE="${compose_files:-docker-compose.yml}${COMPOSE_PATH_SEPARATOR:-:}docker-compose.dev.yml" docker compose up -d --force-recreate app
   ```

6. Check access, then synchronize a small dataset:

   ```sh
   docker compose exec app healthos probe --days 30
   docker compose exec app healthos sync steps
   docker compose exec app healthos status
   ```

For unattended Google synchronization, run the plain `docker compose up -d` (production mode). With
`google` selected it also requires an HTTPS `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` and a
`GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` value; the [deployment guide](../DEPLOYMENT.md) walks through a
server setup with the dashboard behind an SSH tunnel and a webhook-only Cloudflare Tunnel. Google's
Testing-mode refresh tokens expire after seven days; see
[OAuth and Google Health setup](#oauth-and-google-health-setup) above.

**Google history.** Set `BACKFILL_START_DATE` in `.env` to the earliest date you want to attempt,
recreate the app to apply it, then run:

```sh
docker compose exec app healthos backfill --data-type steps
# All configured data types; this can take time:
docker compose exec app healthos backfill
```

Backfills save progress by type and time window; rerun an interrupted backfill to resume it.
Reconciliation and incremental synchronization pick up late-arriving records. For Garmin history,
use `healthos garmin-backfill` ([Garmin](garmin.md#garmin-connect-personal-api)).
