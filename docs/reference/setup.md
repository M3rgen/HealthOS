# HealthOS reference — Setup

Part of the [technical reference](../REFERENCE.md).

## Requirements

- Docker Engine with Docker Compose, or Python 3.12 or 3.13 and PostgreSQL 15+
- `uv` 0.13.6 for deterministic native dependency installation
- A Google Cloud project with Google Health API access, only for the Google source (not needed for Garmin, development or tests)
- For webhooks: a domain/public HTTPS URL that routes to `/webhooks/google-health`

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

If `.env` does not exist, run `make env` to generate its three secrets with mode 600.

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
