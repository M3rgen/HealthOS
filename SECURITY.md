# Security policy

## Scope and supported versions

HealthOS is a personal, single-user health archive. Security reports are welcome for the
application, API, database integrity guards, source clients, backup scripts, deployment defaults
and optional Umay bridge shipped in this repository.

Security fixes target the latest release and the current source tree (**unreleased**).
Older releases have no separate security maintenance branch. Check the [changelog](CHANGELOG.md) and
[upgrade instructions](docs/RUNBOOK.md#upgrade-from-v022) before deploying fixes.
A source fix does not mean a release artifact has been published.

## Reporting a vulnerability

Privately report security vulnerabilities through GitHub's
[Report a vulnerability](https://github.com/M3rgen/HealthOS/security/advisories/new) form with a
synthetic reproduction, affected version, expected and actual behavior, and likely impact.
Use a minimal example: never attach health records, account identifiers, tokens, `.env`,
database dumps, private paths or real-account screenshots. If explaining a finding would
expose sensitive data, describe only the affected component and ask how to provide a sanitized
reproduction. There is no published response-time guarantee.

Use [public issues](https://github.com/M3rgen/HealthOS/issues) only for non-security bugs.

## Known design boundaries

- **Loopback deployment.** The supported UI/API binds to `127.0.0.1`, reached locally or over an
  SSH tunnel. `APP_UI_TOKEN` protects the interface and API; tokenless development accepts
  only loopback client peers. It is not a multi-user authorization system. The public tunnel
  forwards only `POST /webhooks/google-health`, with its own authorization and signature
  checks. Exposing the UI to a LAN or the internet is outside the supported deployment.
- **Database role.** Production refuses a superuser login, a direct or indirect member of the
  schema owner or a privileged role (even with `NOINHERIT`), or a login with `CREATEROLE`,
  `CREATEDB`, `REPLICATION` or `BYPASSRLS`. The dated `HEALTHOS_ALLOW_SUPERUSER_UNTIL` exception
  lasts at most 30 days; direct non-superuser schema ownership alone remains a warning.
- **Host bridge.** Umay is optional. Its authenticated TLS front accepts only the chat's seven
  RPC methods in their allowed order and shape (`initialize`, `initialized`, `account/read`,
  `config/read`, `thread/start`, `turn/start`, `turn/interrupt`) and validated tool responses.
  Account/config reads are once per connection before thread creation; their replies exclude
  account details and host paths. Shell execution and configuration writes are refused.
  Restart the host bridge after updates so its filter changes take effect. A compromised app
  holding the bridge bearer can still initiate allowed chat turns and consume account quota;
  the bridge is not isolation from every action of the logged-in host account.
- **Webhook replay window.** Google's signature covers the body without a signed timestamp
  or nonce. Successfully processed signature/body pairs are suppressed for 24 hours in a
  process-local cache of at most 4096 hashes; copied deliveries, including equivalent signature
  encodings (ECDSA `(r, n−s)` twins and alternate base64 spellings), receive 204 without new writes.
  Restart, eviction or expiry ends that cache protection. Persistent payload deduplication and
  the processor's signed-retry age limit still apply, but a signature alone is not proof of
  freshness. Failed batches remain retryable.
- **Raw archive.** Original source records and versions are preserved; source deletions are
  marked, not propagated as destructive deletions. Backups need independent key custody and
  restore drills. There is no automatic off-site backup.

## Privacy and logs

Operational logs must not contain health values, raw payloads, chat contents or credentials.
They use event names and value-free error diagnostics (exception classes, SQLSTATE and code
locations); credential redaction and transport log suppression provide additional protection.
Umay transfer receipts include counts, date ranges and field names, without record values.
Do not enable verbose transport logging against a real archive or publish production logs.

The archive needs no AI subscription or telemetry. When Umay is used, questions, the user's time
zone and requested archive slices are sent to OpenAI. Chat has no persistent memory in HealthOS.
See [Privacy](docs/PRIVACY.md), [transfer receipts](docs/UMAY_TRANSFER.md),
[UI authentication](docs/UI_AUTH.md) and [deployment](docs/DEPLOYMENT.md) for the full boundaries.
