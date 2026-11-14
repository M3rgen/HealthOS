# HealthOS reference — Troubleshooting

Part of the [technical reference](../REFERENCE.md).

## Troubleshooting

- **`ACCOUNT_NOT_LINKED`**: the selected Google account is not linked to a Fitbit account.
- **No refresh token**: revoke the app's prior grant and repeat OAuth; the flow explicitly requests offline consent.
- **403 on health endpoints**: verify API enablement, test-user access, requested restricted scopes, and consent.
- **Backfill appears slow**: sleep/exercise pages are capped at 25 by Google; progress persists after every page.
- **Webhook subscriber creation fails**: confirm public HTTPS/TLS 1.2+, exact auth secret, both handshake results, and project **number**.
- **Webhook 401**: check the exact authorization header; do not replace Google's public keyset URL with an untrusted key.
- **Webhook 503 `Webhook verification temporarily unavailable`**: Google's public keyset could not be downloaded (`key_download_failed`, log `WEBHOOK_VERIFICATION_UNAVAILABLE`); this is not a signature verdict, Google retries the delivery. Check outbound network access to the keyset URL.
- **`provision-app-role` stops with `this database holds no records`**: a new, empty database needs `POSTGRES_APP_USER` and `POSTGRES_APP_PASSWORD` (or `POSTGRES_ALLOW_OWNER_RUNTIME=1` if you decided the app runs as the owner). An empty earlier install on which the app already started with Google selected is recognised and goes on as the owner ([Application database role](../RUNBOOK.md#application-database-role)).
- **`/health` lists `backup_missing`, `backup_failed` or `backup_status_unreadable`**: no backup has run yet, the last one failed, or its status file cannot be read; `status` is not affected. Install the timer ([deployment step 8](../DEPLOYMENT.md#8-encrypted-backups)) or read the status file ([runbook](../RUNBOOK.md#backup-verify-and-restore-drill)).
- **`/health` lists `database_role_superuser` or `database_role_schema_owner`**: the app connects with more privilege than it needs; see [Application database role](../RUNBOOK.md#application-database-role). `status` is not affected.
- **`migrate` exits with `unsafe or missing production configuration: GARMIN_API_KEY_FILE`**: the Garmin key file is empty or not readable by UID 10001 ([Garmin](garmin.md#garmin-connect-personal-api)).
- **`migrate` exits and the app does not start after an update**: read `docker compose logs migrate` (the app's log has no `Running upgrade` lines); fix the cause and run `docker compose up -d` again, because `migrate` does not restart by itself.
- **Umay reports a bridge error right after an update**: an old plain bridge is still running; stop it and start the launcher or service once ([Restart the Umay bridge](../RUNBOOK.md#restart-the-umay-bridge)).
- **Login answers `429`**: too many attempts for the whole application; wait for `Retry-After` ([throttle](../UI_AUTH.md#login-throttle)).
- **Repeated rows**: run `healthos verify`; database uniqueness is `(data_type, google_data_point_id)` for raw records.
- **Unknown new Google fields**: they are already retained in `raw_json`. Add or revise a parser later and reprocess locally without calling Google again.
