# Changelog

All notable changes to HealthOS are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases before 0.2.1 were not recorded
in this file when they were made; the 0.2 entry was written afterwards from the project history.

> **Version numbers were reset once.** From October 3, 2026 the project's early snapshots were
> labelled 0.5.0. On October 23, 2026 the numbering was restarted at 0.2 and 0.5.0 was retired.
> There is no 0.3, 0.4 or 0.5 release: 0.2 is newer than every snapshot labelled 0.5.0, and the
> releases since are 0.2.1, 0.2.2 and so on.

## [0.2.2.1] - 2026-11-13

The first interim release toward 0.2.3. It covers everything merged since v0.2.2; interim
releases use four-part versions (`0.2.2.N`, tag `v0.2.2.N`), and the frontend manifest writes the
same number as `0.2.2+N`.

### Fifth hardening pass, November 2026

Security and upgrade notes:

- Production refuses a PostgreSQL superuser connection unless `HEALTHOS_ALLOW_SUPERUSER_UNTIL` grants at most 30 days of migration time. Configure `POSTGRES_APP_USER` and `POSTGRES_APP_PASSWORD`; role provisioning rejects membership in owner or privileged roles. `/health` reports temporary superuser grace.
- The dashboard uses `healthos.localhost`. A page request on plain `localhost` or `127.0.0.1` is redirected there before the login form appears, so the key is entered once, unless `APP_PUBLIC_URL` explicitly selects another origin; a `POST /login` on the plain address returns `409` with a link, sets no cookie and records no login. API calls, health checks, static files and the Google OAuth paths are not redirected. The default `GOOGLE_REDIRECT_URI` is now `http://healthos.localhost:8000/auth/google/callback`: register it in the Google Cloud Console before relying on the default, or keep an explicit `localhost` value in `.env`. An explicit `localhost` callback still uses a single-use handoff without a second UI login.
- Tokenless development requires a loopback client peer. A loopback listener alone no longer admits a forwarded remote client.
- The Umay bridge permits account/config reads once per connection before thread creation and returns only required fields, excluding account details and host paths. Identical successfully processed webhook deliveries are suppressed for 24 hours without database writes.
- Production validates database password strength, the HTTPS Google API origin, Fernet roots and exact `APP_ENV` values at startup.
- The secret strength rule no longer rejects the random hexadecimal output of the documented key generators because of chance letter repeats; words repeated behind changing numbers or separators are still refused.
- Retired Google token roots in `GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD` are validated but never used to read tokens, and startup warns when the setting is filled. After upgrading from v0.2.2, and after any change of `GOOGLE_TOKEN_ENCRYPTION_KEY`, Google sync pauses until you use "Connect Google" once; records and backups are kept. If a root key or database dump leaked, revoke HealthOS access at Google before rotating ([steps](docs/RUNBOOK.md#upgrade-from-v022)).
- A Garmin key file with looser permissions logs a startup warning and adds the `/health` reason `garmin_key_file_permissive`; a later release will refuse to start with it.
- Migrations `20261111_0030` to `20261112_0034` add append-only Garmin tables (record observations, heart sample sets, completed daily and activity results) and a singleton table for normalization status. They run in the `migrate` service, only add tables, change no existing row and refuse a downgrade that would discard their contents ([notes](docs/MIGRATIONS.md)).
- The removed command alias `health-mirror` and two unused Compose drafts, `docker-compose.windows.yml` and `docker-compose.linux.example.yml`, are gone. Before upgrading, run `grep '^COMPOSE_FILE=' .env` and drop those two file names from the value if present; otherwise Compose stops on the missing file. Scripts must call `healthos`. The unused `CLOUDFLARE_TUNNEL_TOKEN` line left `.env.example`.

Deployment and publication:

- Application, migration and role provisioning share one project image; only the application service builds it. Google setup preserves existing Compose overrides and Garmin key mounts.
- The backup timer runs at 03:30 UTC. Deployment documents the detailed and application-visible status files, Debian prerequisites, and a copyable systemd override for a separate backup disk.
- The deployment and backup path was verified on Debian 12 on 11 November 2026, including restore drills, Compose smoke and the real backup unit; Debian 13, Google consent, the public tunnel and Umay were outside that run.
- Windows instructions match the supported CLI, application-role password and empty compatibility override. The ufw example restricts Compose-source access to Docker bridge interfaces.
- Public snapshots exclude operational runbooks and the maintainer's branch guide regardless of renamed files. Built-in privacy checks work without local configuration, allow local pattern overrides, and report matching paths without file contents. Public references use contributor guidance, private plan filenames use a generic test pattern, and maintainer publication tools stay outside image builds.
- Releases no longer overwrite existing assets. Release guidance requires independent review and environment approval instead of permanent ruleset bypasses.
- Tool requirements: `uv` must be 0.13.6 or newer within 0.13 (`>=0.13.6,<0.14`; the image uses 0.13.6), and CI and the image share Python 3.12.16. The frontend build uses Vite 8.3.8, which closes the two high advisories in the build tooling, and the clock tests use the pinned `time-machine` package.
- `make env` creates a private `.env` (mode 600) from the template with three generated secrets and never overwrites an existing file; the quick start also prepares the backup status directory before Compose starts.
- `backup.sh` checks the host Python before running, and `restore.sh` reads the backup settings file (`BACKUP_ENV_FILE` or the default path), accepts a relative key path and refuses to start when the PostgreSQL volume lacks free space for a copy of the live database. When the backup helper cannot run, `/health` reports `backup_failed` instead of an old success.
- Release tags may have four parts (`v0.2.2.N`, N at least 1); `make bump-interim` and `make bump-target VERSION=X.Y.Z` advance the five version files and open a new Unreleased section, and refuse to run while one is open.
- CI checks workflows with actionlint and ShellCheck (a finding fails the required summary and stops publishing), and the scheduled audit fails on build-tool advisories. Publication checks stop on exported data files, known file signatures and JSON over 100 KB unless an exact reviewed path and SHA-256 allows it, and never print their contents.
- CI combines SQLite and PostgreSQL coverage under the existing total/security floors and preserves measurement copies. The required bridge boundary job exercises 24 transport cases with a synthetic CLI; real-CLI verification remains separate.

Added:

- `healthos import-fit PATH` and `POST /api/imports/garmin-fit` import a FIT activity file from Garmin or another maker (at most 8 MiB, no Garmin account needed); the result is counters only, importing the same file again adds no duplicate workouts, and an invalid file or failed write leaves nothing behind. A file whose field types do not match the FIT profile is refused.
- Sample data: `healthos demo` loads about 90 days of synthetic data for an invented person (`--remove` deletes it again). An installation without a working source offers **Örnek veriyle dene** in the overview and in Sistem; while it is shown a banner on every page says so and offers **Örnek veriyi kaldır**. Loading is refused once the archive holds Google or Garmin data ([details](docs/reference/sources.md#sample-data-örnek-veri)).

Garmin and data reliability:

- The metric history reaches every page of the Garmin archive. Corrected or re-appearing Garmin content updates the current view: a corrected heart-rate sample, daily value or activity no longer leaves the old value or sport visible. Days left half processed outside the normal window are completed in bounded transactions.
- Malformed Garmin responses (bad lists, JSON errors, repeated activity pages) are no longer counted as a successful pull; over-long durations, duplicate heart-rate timestamps, truncated activity ids and a malformed `Retry-After` header or token expiry no longer stop or corrupt a sync. Token exchange retries are bounded, and a response that echoes the bearer token or key is refused and archived with that value masked.
- After ownership of a data kind is lost, no further raw or normalized write is made. A normalization failure is stored in its own status and degrades `/health` and `/api/status` with `garmin_normalization_failed` until a later normalization succeeds.
- FIT activities from other makers are accepted as their own FIT source, and the activity history no longer runs an extra query per day.
- Webhook notification queries cover at most 256 windows, coverage evidence over long ranges is read in bounded pieces, and webhook key-set downloads are limited to one per 60 seconds and 64 KiB. Two different manual values for the same moment are shown as a conflict in the profile, not silently resolved.

Interface and frontend quality:

- Minute refresh preserves an open monthly history or coverage view and repeats only the submitted coverage selection; returning to a hidden tab refreshes immediately.
- Garmin-only connection and System panels show pull state, explain unavailable manual sync, and list Garmin data types. Source names and server error messages follow the interface language.
- Failed loads offer one retry, disable unavailable sync/export actions and clear stale profile values; initial loading has an explicit indicator.
- System status/count columns and mobile dates stay visible; history day labels, 200% text step cards, short-phone rows, pointer targets and the Umay button preserve usable space without page scrolling. Transfer and coverage text is simpler.
- Text overflow/collision checks measure visible text in both languages across viewport and text-size matrices. Build budgets document the baseline and 5% target headroom while retaining historical ceilings; shared width breakpoints preserve compiled CSS.
- The login page offers Turkish and English; without a saved choice the interface, too, uses the browser's language (Turkish or English, English for any other). A failed login keeps the chosen language and the safe return address.
- Forms record measurement and workout times in the time zone chosen under **Görünüm** (a skipped daylight-saving hour moves forward, a repeated hour takes the earlier one); **Kaydet** and **İptal** stay visible in the workout form; the measurement list says it shows the newest 100 entries.
- A dashboard load error is no longer hidden by a sync error in the same round, and cards that could not refresh show the time of their last good data.
- On phones the overview opens with the Steps card and the 3D body model loads only when Profil is shown; history rows fit narrow and landscape phones and at 200% text, and checkboxes and radio buttons have at least a 24 by 24 px target on desktop.
- Explicit JSDoc `any` escapes are replaced with data/DOM/figure contracts. Preview captures use fixed viewports, languages, clocks and preferences; hidden or stationary figures release their animation loop.

Documentation:

- Flat `app.services.<module>` imports raise a `DeprecationWarning` and are planned for removal in 0.2.4; use the sub-package paths. Contributors also have guards for layer directions, sub-package edges and session-lock cleanup.
- Security reports go to the private reporting form; public issues are for non-security bugs. Operational examples pass tokens through standard input instead of process arguments.
- A security policy records reporting, supported-source status, privacy and design boundaries. Architecture and bridge details share one canonical document with compatibility links; the layer map reflects implemented dependencies and guard coverage.
- Installation and runbook checks use current backup health metadata instead of relying on a potentially stale legacy success. README includes the existing-installation migration path, and verification status statements carry dates.

### Fourth hardening pass, November 2026

Upgrade notes:

- Production now wants secrets as strong as generated ones: `APP_UI_TOKEN`, `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` and a configured `CODEX_BRIDGE_TOKEN` need at least 32 characters and no common password ([rule](docs/reference/config.md#secret-strength)). A secret that passed the previous 16-character rule stops startup with a message naming it; `HEALTHOS_ALLOW_LEGACY_SECRETS=true` keeps it, with a warning at every start, until you replace it. The bridge launcher applies the same rule.
- The bridge launcher accepts as `CODEX_BRIDGE_BIND_HOST` only loopback and Docker bridge addresses (`docker0`, Compose `br-*`); any other address needs `CODEX_BRIDGE_ALLOW_NON_LOCAL_BIND=1`.
- Bridge service: keep the Python that `uv` downloads inside the checkout, `UV_PYTHON_INSTALL_DIR=/opt/healthos/.python uv sync --frozen --no-dev`, and reinstall the unit. A copy in a home directory is hidden from the service; the new unit names this fix in the journal instead of failing with a bare `203/EXEC` ([steps](docs/DEPLOYMENT.md#9-optional-the-umay-bridge)).
- `/health` may list `backup_missing`, `backup_failed` or `backup_status_unreadable`; `status` does not change. `webhook_notifications.status` may hold the new value `interrupted` (no schema change), which `healthos webhook-status` counts.

Security:

- The bridge's connections ignore proxy environment variables. Connections that have not shown the bearer wait in their own queue of 16 (the oldest is dropped when it is full) for at most 5 seconds, and a TLS handshake gets 5 seconds, so they cannot lock the chat out.
- Garmin responses are read under byte and time budgets: 64 MiB on the wire, 32 MiB decompressed, 1 MiB for the token exchange, 120 seconds per body. A refused body is archived as incomplete, not parsed and not retried ([details](docs/reference/garmin.md#garmin-connect-personal-api)).
- Umay's answers keep a link only when it points to this site; a link elsewhere becomes plain text ([Privacy](docs/PRIVACY.md#umay-and-openai)).

Data and reliability:

- Flagging the same measurement concurrently records one event, not several.
- The history page reads only the metric it shows (heart rate: about 88 MB of reads down to 1.5 MB on a synthetic archive).
- `POST /backfill/start` while the same selection is running joins that run instead of starting another.
- The single-run lock no longer names a process that holds a lock in another database.
- Google and Garmin implement one typed provider contract; incremental sync and backfill share one page cycle; an interrupted webhook element has its own status and archive scans read in pages; the choice of database dialect lives in one module, and the application installs only the PostgreSQL adapter (SQLite stays a test fixture). Runner #336 to #339.

Deployment and backups:

- `make compose-smoke` does not read `.env` and removes only the images it built.
- PostgreSQL gets 60 seconds to stop.
- Upgrading an earlier install that is still empty no longer stops at `provision-app-role` when an app start is recorded in it.
- The Sistem page shows **Son yedek**; `/health` reports `last_backup_success`.

Interface:

- Error, empty-archive and notice states no longer make the main page scroll; the browser checks also measure `main` and the visible page.
- A part of the page that cannot load shows an error screen with a reload button; if the English text fails to load, the page opens in Turkish with a notice.
- At 200 % text the selected day stays in the top bar; **Ölçüm ekle** is visible in an empty archive.
- Without Google, the Sistem page hides the Google panels and the empty-state button matches the setup.
- The frontend type check runs in full `strict` mode with an empty baseline.

Quality:

- New `e2e` CI gate, required by `summary`: `make test-e2e` drives the built frontend against the real app and PostgreSQL ([development](docs/DEVELOPMENT.md#validation)).
- The release and CodeQL gate tests run the workflow steps themselves.
- A weekly run in random order (`make test-order`, in `clock.yml`).
- `audit.yml` and the development guide say that a Vite advisory can reach the shipped bundle.

Not proven: any GitHub-hosted or `x64` run (`e2e` ran under `act` on `arm64` only), `release.yml`, the fifteen opt-in real-CLI tests, the bridge's full start with the real CLI under the new unit, Debian, real phones, Safari and screen readers. The 200 % step of the viewport matrix failed once in three `act` runs and could not be reproduced.

### Final hardening pass, November 2026

Upgrade notes:

- Restart Umay's bridge after updating: its front now passes only the chat's seven RPCs, and an older front keeps forwarding everything until it is restarted (the chat refuses to use it). From now on a change to the persona, health-tool schema, filter or policy needs a restart: the bridge fixes them at start, and the chat refuses a bridge verified under another version ([steps](docs/RUNBOOK.md#update-the-application)).
- Reinstall the backup units and run `daemon-reload`: the new unit reads the deploy user's `.env` through `LoadCredential=` (systemd 247+) and takes `backups/` and `backup-status` for root on every run. After that, reading `backups/` needs `sudo`. Hosts with a custom checkout or backup paths must also adjust `LoadCredential=` and the `chown` ([details](docs/DEPLOYMENT.md#8-encrypted-backups)).
- A mistyped `GARMIN_API_KEY_HOST_FILE` now stops `docker compose up` instead of making Docker create a root-owned directory.
- `/health` may report `degraded` with a Garmin reason it did not report before; `garmin_pull_uninitialized` is expected until every kind has succeeded once.

Security:

- Umay's bridge front terminates the WebSocket and passes only the chat's seven RPCs, in their exact shape. `command/exec`, configuration writes and any other thread, reachable with the bearer before this (reproduced on the real CLI 0.161.0), are refused; replies to the app-server's requests are filtered too.
- Dashboard inputs with NUL, invalid Unicode, UTC overflow or oversized numbers get a `422` instead of a `500`. Login attempts log a fixed `LOGIN_ATTEMPT` event with only the client class and the result. The secret strength check catches repetition hidden behind changing numbers or separators.

Deployment and backups:

- The scheduled backup works with the documented `.env` ownership, its capability set still empty, and `/health/backup` updates after a timer run. `make test-backup-unit` runs the unit under real systemd.

Database and reliability:

- A data-losing downgrade is refused at one point for every revision, including `alembic downgrade base` with rows present ([notes](docs/MIGRATIONS.md#downgrades-that-would-lose-data-are-refused)).
- Webhook deliveries wait for a free slot instead of counting as running (six concurrent deliveries: all six finish, was two), and the new `webhook-unfinished` job finishes what a shutdown left behind, every five minutes.
- Each Garmin kind has its own PostgreSQL ownership: a first-row race and stale writers are gone, and a CLI backfill makes the scheduled pull wait for that kind. Garmin normalization runs in seven-day transactions.
- Garmin health is on `/health` and `/api/status` (last success, fixed error code, lag, three reasons). Failed pulls report; automatic recovery stays Google's.
- Sync and webhook errors carry a typed HTTP status and error code instead of parsing message text, and parameter stacks became value objects (runner #328, #329 added tests for uncaught mutations).

Interface:

- Polling keeps scroll position, open rows and focus; live regions are written only when they change.
- The Sistem page is four groups that fit the screen, with tabs on small screens. With desktop Umay open, Tab no longer stops on hidden controls.

Quality and repository:

- Preview fixtures are compared with real API responses, and a real drift (a measurement `value` that was a number in the fixture) was fixed. A release image is compared with the one the smoke job tested, and `publish` runs in the `release` environment.
- The clock matrix works again, and a broken harness now fails the blocking `verify` job (`make test-clock-preflight`). Event-loop tests count loop turns instead of wall-clock time; cancellation-budget tests wait for the archive write. `codex_runtime_rpc.py` counts toward coverage.
- Frontend functions are limited to 60 lines and complexity 10, and `tsc --strict` errors went from 469 to 9 (all three.js typings).

Not proven: any run on GitHub-hosted runners or `x64` (`act` is `arm64`, and skips CodeQL and `smoke`, which were run by hand on the host); `release.yml`, the `release` environment and attestation; a real ChatGPT turn through the filtered front (run with the real CLI and a synthetic provider); Linux and systemd for the bridge; the fifteen opt-in real-CLI tests in this combined tree; a real Garmin account; real screen readers and phones; Debian (the deployment guide was followed on Ubuntu 24.04). `npm audit` over the build tooling reports six advisories (two high, vite), left to the maintainer. The 8 MiB frame limit of the bridge front is a judgement, not a measurement.

### Second hardening pass, November 2026

Upgrade notes:

- Umay's bridge now needs one restart after updating: the app connects only over TLS 1.3 to a certificate pinned to `CODEX_BRIDGE_TOKEN`, and refuses an old plain bridge with a clear message. `.env` stays as it is (`ws://` is dialled as `wss://`). See [bridge upgrade](docs/DEPLOYMENT.md#9-optional-the-umay-bridge).
- A new, empty database now stops at `provision-app-role` without `POSTGRES_APP_USER` and `POSTGRES_APP_PASSWORD` (`POSTGRES_ALLOW_OWNER_RUNTIME=1` overrides it). Existing archives keep running as the owner, with a startup warning and a `/health` reason.
- Production now refuses weak secrets beyond length and one repeated character ([strength check](docs/reference/config.md#secret-strength)). A token that passed before can be refused now.
- Upgrading from v0.2.2 works: the empty revision `20261107_a001` and the merge `20261109_0029` join its database to this tree, and its `apple_*` data is untouched ([path](docs/RUNBOOK.md#upgrade-from-v022)).

Security:

- Umay's bridge proves its identity with TLS; an impostor on its port gets neither the bearer token nor a message.
- Login is throttled across the whole application (`429` with `Retry-After`); logging out closes an open Umay chat immediately; health responses are `no-store`.
- Plain `localhost` and `127.0.0.1` sessions last 24 hours, `healthos.localhost` and HTTPS stay at 30 days, because a cookie cannot be scoped to a port.
- The login redirect is rebuilt from validated parts so code scanning can see it, and a table of hostile `next` values (backslash, encoded and control-character spellings) is tested end to end.

Deployment and backups:

- `healthos verify` no longer reports tampering after an honest restore and still catches a real change; a false guard warning after restoring a live archive is gone.
- `make backup` reads `/etc/healthos/backup.env`, runs with `sudo` when it must, and matches the timer. `make restore` needs `BACKUP_AGE_IDENTITY` or a key file for `.age` files.
- The app container receives only the settings it reads; `.dockerignore` and `.gitignore` exclude `secrets`.
- The development override really publishes PostgreSQL on `127.0.0.1:5432`.

Reliability:

- A run waiting for its turn no longer holds up other data types' turns, and write collisions retry with jitter. The connection pool grew to `3 × SYNC_CONCURRENCY + 3` (9 at the default).
- A real backup and restore round trip is tested against PostgreSQL 17.

Interface:

- A server error is no longer shown as an empty archive; pages have their own address, so Back, Forward and reload work; about 456 KB less to download on a phone; heart rate is shown as "atım/dk".
- Layout follows the browser's font size; charts are at least 40 px tall on laptops; the history panel drags on landscape phones; one retry control per failure.
- ESLint size limits and a `tsc --strict` ratchet for the frontend.

Quality and repository:

- CodeQL and a Compose smoke run block CI; daily dependency audits, a weekly clock matrix; a release verifies its own provenance before publishing.
- The runbook keeps operations only; what each migration changed moved to [Migration notes](docs/MIGRATIONS.md). Facts checked against the code; `README` no longer says the interface is Turkish-only, and Turkish text moved to `*.tr.md`.

Not proven: a real ChatGPT login and turn through the bridge front (run with the real Codex CLI 0.161.0 and a fake login); any run of these workflows on GitHub Actions, the rulesets, `publish` and attestation; a real Garmin account in this pass; real phones, Safari, Firefox and screen readers. About 470 frontend strict-mode errors remain under the ratchet. The clock matrix and a few timing-budget tests failed once each at high machine load and passed alone.

### Hardening and verification, November 2026

Dependency and image upgrades: `certifi` 2026.7.22 → 2026.10.18; `cryptography` 46.0.7 → 50.0.0 (cap
now `<51`, resolving four pip-audit advisories; tokens written with 46.0.7 were read back on 50.0.0 and vice versa);
`dompurify` 3.4.16 → 3.4.17; new dev dependencies `pytest-timeout` and TypeScript 5.9.3; Node build
image 24.21.0 → 24.22.2; Python runtime image 3.12.13 → 3.12.16. Rebuild images and rerun `npm ci`.

Database:

- Migration `20261108_0028`: derived-day invalidation is deferred to commit and applied in key
  order, so concurrent syncs no longer deadlock on the same days; a page transaction aborted for a
  deadlock or serialization failure is retried, four attempts in total (tested on PostgreSQL 16.2, not 17; a
  three-party deadlock with the derived-day builder is still possible and only writers are retried). Large pages write about 12 times
  faster, and Garmin writes no longer invalidate Google days.
- Optional non-superuser application role (`POSTGRES_APP_USER`, `POSTGRES_APP_PASSWORD`, the new
  `provision-app-role` service): the schema owner still migrates, and the append-only triggers are out of the
  app's reach. New installs set it through `.env.example`; existing installs change only after adding both lines ([runbook](docs/RUNBOOK.md#application-database-role)).
  `docker compose exec app alembic upgrade` is no longer the path: use the `migrate` service.

Deployment and security:

- Production refuses a weak or placeholder `APP_UI_TOKEN`, `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` or configured
  `CODEX_BRIDGE_TOKEN` (under 16 characters or one repeated character), and a `CODEX_BRIDGE_URL`
  outside loopback or the Docker gateway.
- Google OAuth uses PKCE (`S256`).
- `/backfill/start` and `/sync/*` spend the manual-write budget and return `429` when it is used up.
- CLI failures print only the error class; logs are redacted. Google and Garmin commands now say that
  their source is off (`HEALTHOS_SOURCES=none`), and selecting an uninstalled `apple` logs a warning, adds a `/health` reason and shows "Kurulu değil".
- Compose: `docker-compose.garmin.yml` enables Garmin without editing tracked files (key owned by UID
  10001); `APP_PORT` in `.env` no longer moves the container listener; the Docker user is
  `healthos` (UID 10001 unchanged) and `/health` reports `service: healthos`; Caddy is read-only with dropped capabilities; every
  service has memory and process limits; the backup unit creates `/opt/healthos/backups` itself; systemd units hardened (`backup` 1.9, bridge 3.9 by `systemd-analyze security`).

Interface:

- Every page fits one screen down to 320×256; type follows the browser's font-size preference; 44 px touch
  targets, 12 px minimum text, skip link; empty and error states for Measurements and Overview.
- Full-screen Umay sheet on phones (modal, focus kept inside); history panel resizes by keyboard.
- `main.js` split; a malformed response no longer locks the panel; notices offer **Yeniden dene**;
  WebGL context loss shows a silhouette; English translation complete; data types and dates
  localized; `checkJs` required in lint.

Quality gates:

- CI runs `make coverage-security` and `make test-postgres`, including the whole unit suite on PostgreSQL;
  gates are protected by mutation tests (`tests/test_ci_gates.py`). Release needs a tag matching the
  version, on `main`, with successful CI on that commit, then attests provenance. `make ci-gate`
  records a local receipt the pre-push hook requires; rulesets and CODEOWNERS ship in `.github/`.
- `pytest-timeout` fails a test after 60 seconds; `make test-postgres`, `make test-clock` and the
  `local_sockets` fixture (`HEALTHOS_REQUIRE_SOCKETS=1`) are new. Pyright is `standard`; Ruff `C901` ≤ 24 (lowered to 16 later this month).

Not yet proven: no run of these workflows on GitHub Actions, rulesets not applied, `publish` and
attestation never executed. `npm audit` over dev dependencies reports vite and source-map-js (production: none). Tested only in
Chromium: real phones, Safari and screen readers were not. The Apple importer's presence was stubbed.

### Documentation and first run

- `.env.example` now sets `HEALTHOS_SOURCES=none`: a fresh `.env` boots in production mode with
  only `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD` and `APP_UI_TOKEN`. An existing `.env` is not affected; a `.env` with
  the line removed still means `google`, plus Garmin with `GARMIN_ENABLED=true`.
- README: a tested Garmin-only quickstart, and production no longer described as requiring Google.
- New reference page, Sources: what each `HEALTHOS_SOURCES` value does, and what is not in this
  tree. Apple Health is labelled as absent from this tree; `HEALTHOS_SOURCES=apple` accepts the
  value and imports nothing.
- Corrected: migrations run in the one-shot `migrate` service, not at app start; the Umay bridge
  has a shipped systemd unit; `docs/reference/config.md` lists every setting and template variable.
- English: `docs/UI_AUTH.md`, `docs/UMAY_INTERPRETATION.md`, `docs/UMAY_TRANSFER.md`,
  `docs/PERFORMANCE_BUDGET.md` and project planning notes; the dated backfill guide is marked
  as Turkish documentation without changing its contents.

### Version

- `pyproject.toml` is the single version source, and the app reads its version from the
  installed project metadata (`app/core/version.py`); `tests/test_version_agreement.py` keeps
  `pyproject.toml`, `uv.lock`, `frontend/package.json` and `frontend/package-lock.json` in step. The
  declared version is now 0.2.2.1, the first interim release toward 0.2.3.

### Upgrade notes

- The Umay bridge now starts only with a Codex CLI release it has been verified against
  (0.161.0). Before it starts it measures the CLI in an isolated probe (about 15 seconds); with
  another release, or when the probe fails, the bridge stays off and the rest of HealthOS works
  as before. A bridge started by the earlier launcher is not reused: restart it once. The new
  optional `CODEX_BRIDGE_CLI` (absolute path) selects the CLI binary.
- After every HealthOS update, restart the Umay bridge. The chat now compares the running
  bridge's effective settings with the bridge policy (including a fingerprint of the policy and
  probe code) and refuses to open a thread on a bridge that does not match, so Umay stays off
  until the bridge is restarted. The bridge no longer inherits the operator's environment: only
  an allow-list plus proxy and CA variables reach it, and its home is `healthos-bridge/home`.
- Development: `pytest-cov` and `pytest-xdist` are new dev dependencies (`uv sync --extra dev`).
  `make test`, and so `make verify`, runs the unit tests in parallel; `pytest -q -p no:xdist`
  runs them in one process.
- Development: the interface has a linter, `cd frontend && npm run lint` (ESLint with
  `no-unsanitized`). The new pinned dev dependencies need Node ^20.19, ^22.13 or 24+; the
  Dockerfile and CI already use 24. `frontend/package-lock.json` changed, so cached
  `node_modules` keyed on it are rebuilt.

### Security

- Error logs no longer carry health records, source or device names, SQL parameters or the
  database's DETAIL text (CWE-532). Backfill and identity-reconciliation failures keep only the
  exception's class chain, SQLSTATE and code locations, in the log as in
  `identity_reconcile_state.last_error`. Every log record, APScheduler's and uvicorn's included,
  leaves without its traceback; background job failures log where they happened
  (`module:function:line`). Earlier log copies and stored error summaries are unchanged.
- Garmin: the token exchange's body (the bearer token, or an error answer that may echo the key)
  is no longer stored in `garmin_api_responses`; the row keeps `[redacted: garmin bearer token]`
  while `body_sha256` and `body_size` still describe the bytes Garmin sent (CWE-312). Response
  bodies that contain the token in use, and response headers that carry credentials, are masked
  too. Rows written earlier stay as they are (the archive is append-only); their tokens expired
  24 hours after they were issued. No schema change.
- Umay bridge: the model can no longer read files or images outside the health archive or start
  sub-agents, and the chat sends no text to a thread whose CLI release, model, approval policy
  or sandbox differs from the verified one. `healthos-bridge.service` hides `.env`, the backups
  and `/etc/healthos` from the bridge account.
- Umay bridge: every thread repeats all bridge limits (sub-agents capped, image viewing and the
  other disabled features off), and a bridge that cannot prove its settings through
  `config/read` gets no user text. The launcher measures and runs the same native Codex binary
  (shell shims are skipped; npm installs use the package's own binary) and reports a stale
  bridge or a busy port by PID instead of failing silently.
- Logs: a traceback embedded in a plain log message (as uvicorn does for lifespan errors) is cut
  to its first line and the exception class.

### Fixed

- Interface: a page restored with the browser's Back button no longer stays frozen; it fetches
  fresh data at once, restarts the 60-second refresh and brings the 3D model (or its silhouette)
  back.
- Interface: notices can be closed; success notices leave after 5 seconds instead of 20, and an
  error clears itself when the same request next succeeds. The notice box is smaller.
- Umay: a streaming answer is drawn at most once per frame and turns into Markdown when it ends;
  a malformed frame is skipped without dropping the connection; links in answers open in a new
  tab.
- Interface: a day with an unknown status reads "Bilinmiyor"; every value in the coverage and
  device panels is escaped.

### Changed

- Documentation: `docs/REFERENCE.md` is split by topic into `docs/reference/`; the old page is now
  a table of contents that keeps every old section anchor. The reference and `docs/RUNBOOK.md` are
  in English, and both use the `healthos` command name throughout.
- Tests: `make coverage` enforces a coverage floor for the unit suite (92.6%; 93.7% measured) and
  `make coverage-security` 90% for the security modules. PostgreSQL race tests wait for the
  observed lock instead of fixed sleeps, and tests that compute "today" run on a fixed clock.
  Identity test files are named after what they test; helpers shared by test modules live in
  `tests/support/`.
- The bridge's real-CLI regression runs with
  `HEALTHOS_CODEX_CLI=/path/to/codex pytest tests/test_codex_bridge_runtime.py`.
- Interface accessibility: the heart and sleep accent colours are lighter (at least 5:1 on small
  text), disabled buttons use their own colour tokens instead of fading, a finished Umay answer
  is announced to screen readers with a short summary, and the notice is a labelled status.
- Interface: up to 900 px wide the overview cards are tabs (Profil, Bağlantı, Adım, Uyku, Nabız)
  so neither the page nor the card column scrolls on phones and tablets.
- Interface code: the CSS is formatted one rule per line, its precedence comes from
  `@layer base, components, overrides` rather than import order, and 117 duplicate rules were
  merged without changing what is drawn. Shared state and formatters live in `state.js` and
  `format.js`. Umay's Markdown and sanitiser libraries load on the first connection, which cuts
  the initial JavaScript from about 58 KB to 38 KB (gzip).

### Added

- System › About: HealthOS is not a medical device; HRV and SpO₂ are device estimates; the data
  stays on your own server; what Umay sends to OpenAI when it is on (the Turkish summary of
  [Privacy](docs/PRIVACY.md)).
- The interface is ready for translation: all text is in one dictionary, and System › Appearance
  › Dil offers English (the main dashboard is translated, other pages fall back to Turkish).
  Turkish stays the default and its text is unchanged.

### Product name: HealthOS (prepared 2026-11-07)

One product name, HealthOS, everywhere. The rename itself needs no migration and changes nothing in
the database.

Upgrade notes:

- The command is now `healthos` (`docker compose exec app healthos status`). The old name,
  `health-mirror`, was a deprecated alias in this prerelease and has been removed; scripts must call `healthos`.
- The Python distribution is renamed from `personal-health-mirror` to `healthos`. Docker users
  rebuild the image as usual; a native install picks up the new name with `uv sync`.
- Names a running installation depends on are unchanged: the container user, the default
  webhook subscriber id (`GOOGLE_HEALTH_WEBHOOK_SUBSCRIBER_ID`), the `service` field of
  `/health`, and the systemd unit names in `deploy/systemd/` (always `healthos-*`). launchd
  agents exist only in the guided installer of the published release, which is not part of this
  source tree; it replaces its old `org.healtos.*` agents (below).

Changed:

- The misspelled product name is corrected to HealthOS (the repository was renamed to
  `M3rgen/HealthOS`; the old URLs redirect). Browser preferences and uploaded appearance images
  stored under the old `healtos` names are carried over automatically on first load; the old
  copies are left in place.
- Installations made with the guided installer (GitHub release; not in this source tree) are
  carried over by running the installer again: settings and key directories under the old name
  are copied to the new one and the originals kept, and the old `org.healtos.*` launchd agents are replaced, their plists
  kept as `.bak-<UTC stamp>` files.
- The CLI's help, the API title and the documentation use HealthOS and `healthos`; a test keeps
  the old names to the places that need them for compatibility or history.

Added:

- [Architecture](docs/ARCHITECTURE.md): the layers and their import rules, the data flow of
  every source, why each source keeps its own raw tables and why the record-identity layer is
  as large as it is.
- [Privacy](docs/PRIVACY.md): HealthOS is not a medical device (HRV and SpO₂ are device
  estimates); where the data lives; exactly what Umay sends to OpenAI; a GDPR/KVKK note. The
  README's privacy section carries the short version.

## [0.2.2] - 2026-11-07

### Changed

- The product name, misspelled "HealtOS" up to 0.2.2, is spelled **HealthOS** everywhere: in the
  application, the dashboard, the installers, their scripts and the documentation. The repository
  is `M3rgen/HealthOS` (old GitHub links redirect). Browser preferences and uploaded appearance
  images stored under the old `healtos` names are carried over automatically on first load; the
  old copies are left in place.
- Installed machines keep working when the installer is run again; no data, key or backup is
  moved or deleted
  ([details](docs/INSTALL.md#updating-an-install-from-022-or-earlier-the-old-name-healtos)):
  an existing `~/healtos` checkout stays the default while there is no `~/healthos`; files in
  `~/.config/healtos`, `~/Library/Application Support/HealtOS` and `%APPDATA%\HealtOS` are copied
  (mode 600 / owner-only ACL) to the `healthos` / `HealthOS` directories and the originals kept;
  `backup.sh` and `backup.ps1` fall back to the old key path while the new one is missing; the
  launchd agents `org.healtos.*` are replaced by `org.healthos.*` (old plists kept as
  `.bak-<UTC stamp>`); the Windows tasks `\HealtOS\HealtOS …` are replaced by
  `\HealthOS\HealthOS …`; `HEALTOS_REPO_URL` still works, with a note, next to the new
  `HEALTHOS_REPO_URL`. The installer version marker is now `HEALTHOS_INSTALLER_VERSION`.

## [0.2.2] - 2026-11-07

### Installation

Added: a guided, bilingual (English / Türkçe) installer for every platform
([docs/INSTALL.md](docs/INSTALL.md), Turkish: [docs/INSTALL.tr.md](docs/INSTALL.tr.md)).

- `install.sh` (Linux, Raspberry Pi, NAS, cloud VPS, macOS; also macOS's bash 3.2) and
  `install.ps1` (Windows PowerShell 5.1 and PowerShell 7) take the same options (`--platform`,
  `--mode trial|full`, `--sources google,garmin,apple|none`, `--dir`, `--ref`, `--backups`,
  `--autostart`, `--umay`, `--yes`, `--dry-run`, `--no-start`, …) and the same exit codes (0 ok,
  1 failed step, 2 bad arguments or unsupported environment). One-line bootstrap with
  `curl … | bash` or `irm … | iex`; outside a checkout the installer clones the repository at
  `--ref` (default `main`).
- The wizard lists every platform with a note each: Linux server / VM (default and preselected),
  Raspberry Pi / ARM64 (64-bit OS only), NAS (Synology, QNAP, Unraid, TrueNAS SCALE), macOS
  (Docker Desktop, OrbStack or Colima), Windows 10/11 (Docker Desktop with WSL 2) and cloud VPS.
  On macOS and Windows it preselects the detected host.
- Data sources are configured in the same run: Google Health API v4 (client ID and secret, a
  generated Fernet key, the redirect URI printed), Garmin Connect Personal API (`GARMIN_ENABLED`,
  key file mounted read-only through the new `docker-compose.garmin.yml`), Apple Health
  (`export.zip` upload needs nothing; the Health Auto Export receiver stays off unless chosen,
  and then gets its token file and the new `docker-compose.apple.yml` mount), or none.
- `trial` mode adds the development override; `full` mode lists the production values that are
  still missing before anything starts. The Compose files are selected through `COMPOSE_FILE` in
  `.env`, so plain `docker compose …` uses the same files afterwards.
- `.env` is copied to `.env.bak-<UTC stamp>` before every change; then missing or empty keys are
  filled, `COMPOSE_FILE` follows the current choices and a chosen source's `GARMIN_ENABLED` /
  `APPLE_RECEIVER_ENABLED` is turned from `false` to `true`. An existing secret is never replaced
  and only the few values listed in [docs/INSTALL.md](docs/INSTALL.md#what-the-installer-changes-and-what-it-never-does) can change. A re-run keeps the previous mode unless `--mode` is given, and an explicit `--garmin-key-file` that does not exist fails with exit code 2. Secrets are generated
  without Python on the host. The installers never run `docker compose down -v`, never remove a
  volume and never delete a backup or any other file; running them again is safe.
- Host jobs on every OS: the daily encrypted backup (systemd timer on Linux, launchd agent on
  macOS, Task Scheduler task with the new `scripts/backup.ps1` and `scripts/restore.ps1` on
  Windows; same semantics as `scripts/backup.sh`: kept forever, free-space guard,
  `last-backup-status.json`), autostart after a reboot, and the optional Umay bridge as a
  service (systemd unit, launchd agent or scheduled task). The installer offers to create an age
  identity outside the checkout and `.env` (`/etc/healthos/backup.key` or
  `~/.config/healtos/backup.key` on Linux, `~/Library/Application Support/HealtOS/backup.key`
  on macOS, `%APPDATA%\HealtOS\backup.key` on Windows; these were the names before the rename
  to HealthOS, see Unreleased) and warns to keep a copy off the host.
- The systemd units are rendered with the checkout directory chosen by the installer instead of
  a fixed `/opt/healthos`; their behaviour is unchanged. Without root the installer installs them
  as user units (`systemctl --user`, lingering on); new launchd templates are in
  `deploy/launchd/`, Task Scheduler helpers in `deploy/windows/`.
- `scripts/backup.sh` also runs on macOS (bash 3.2, BSD tools) and uses the first Python 3.11+
  it finds (`BACKUP_PYTHON` overrides); its behaviour is otherwise unchanged.
- All pinned images run on amd64 and arm64. 32-bit ARM (armv7) is not supported.
- CI runs the installers' dry-run tests on Ubuntu, macOS and Windows, and one real trial
  installation with Docker on Ubuntu.
- The README starts with the installer, a platform table and a data-source table, and explains
  how to let a coding agent run the installation ([docs/AI_INSTALL.md](docs/AI_INSTALL.md)).

### Upgrade notes

- Migrations `20261102_0022` and `20261102_0023` run automatically when the app starts and only
  add tables: no existing table is altered or referenced by a foreign key, so they take no lock on
  existing tables and a running `pg_dump` neither blocks nor fails them. No existing row is read or
  changed.
- `20261102_0022` adds the single-row `display_preferences` table (operational metadata, not
  received data); its downgrade drops it, and each browser falls back to its local copy.
- `20261102_0023` adds three append-only tables; its downgrade refuses while any of them holds a
  row.
- `20261107_0024` adds the three Garmin tables (`garmin_api_responses`, `garmin_raw_records`,
  `garmin_pull_state`); only new tables and their guards, no existing table is altered or
  referenced. Its downgrade refuses while either archive table holds a row. Nothing changes
  for a deployment without `GARMIN_ENABLED`.
- `20261107_0025` adds two columns to each of the fifteen normalized tables (`steps`,
  `heart_rate`, `sleep_sessions`, `exercise_sessions`, …): `provider` (`GOOGLE` for every existing
  row through the column default; `GARMIN` for rows normalized from the Garmin archive) and
  `source_record_id`. Only `ADD COLUMN`: on PostgreSQL a constant default is a catalog change
  (no table rewrite, a momentary lock per table), on SQLite the triggers and `analytics_*` views
  are kept (a table rebuild was measured to fail on them, so `google_data_point_id` keeps its NOT
  NULL and holds a Garmin row's `garmin:<kind>:<key>` key). No row is read or changed. Its
  downgrade drops the columns and refuses while a non-Google row exists. The two new settings
  `SOURCE_PRECEDENCE_ACTIVITY` and `SOURCE_PRECEDENCE_HEALTH` have defaults; nothing changes for
  a deployment without Garmin data except the new `sources`, `precedence`, `priority` and
  `alternatives` fields in the dashboard, workouts and devices responses.

- `20261107_a001` (GitHub version only: the Apple Health extension) adds four tables
  (`apple_payloads` and `apple_raw_records`, append-only; `apple_imports` and `apple_sources`,
  bookkeeping) after `20261107_0025`; only new tables and their guards, no existing table is
  altered or referenced. Its downgrade refuses while either archive table holds a row. Nothing
  changes for a deployment that never imports Apple data: the receiver is off by default.

### Fixed

- The nightly identity reconciler no longer gives up on a data type the daily Google
  reconciliation is still storing (#262). Started at a fixed 02:30 UTC, it met heart-rate's lock
  held for 20 to 60 minutes of the 7-day run and gave up after 8 attempts, three nights in a row,
  leaving heart-rate's renamed sources unlinked. It now starts when the scheduled reconciliations
  of the night have ended (the daily run, on Sunday also the weekly one; at
  `IDENTITY_RECONCILE_HOUR_UTC`:`IDENTITY_RECONCILE_MINUTE` only with `RECONCILIATION_ENABLED=false`),
  and a data type still busy (any long sync of it: a reconciliation, the watchdog's recovery
  after a restart, a sync by hand) is tried again after the others until the window ends
  (`IDENTITY_RECONCILE_UNTIL_HOUR_UTC`/`_MINUTE`, 03:55 UTC) or, for a run started later and for
  `identity-backfill` by hand, for `IDENTITY_RECONCILE_LOCK_WAIT_MINUTES` (60; `0` keeps the 8
  attempts), backing off up to 60 s; each attempt keeps the lock contract (0.1 s without
  queueing, 1 s in the queue, 2 s per row lock). A
  data type that still fails is listed in `/health` under `identity_lock_contention` with its
  `nights` (a warning; `status` is unchanged) and in `health-mirror status` as `failed_nights`.
  No migration
  ([docs/REFERENCE.md](docs/REFERENCE.md#reconciliation)).
- A page that repeats an earlier `nextPageToken` of the same fetch no longer loses its records:
  they are stored in `raw_health_data` with their Google responses, deduplicated as usual, while
  the repeated token is never saved as a checkpoint, `last_successful_sync` does not advance and
  the fetch still fails with `repeated_page_token`. Sync and backfill behave the same; no
  migration ([docs/RUNBOOK.md](docs/RUNBOOK.md), “Kapsama ve çekim fişleri”).
- One Google record that PostgreSQL `jsonb` cannot hold (`\u0000`, an unpaired surrogate,
  `NaN`/`Infinity`, an overflowing number) no longer blocks its whole data type. The page used to
  roll back on every attempt, so that type never advanced. Such a record is now set apart in
  `raw_health_data_rejections` (never sanitized, nothing already stored is touched; its bytes stay
  in the page's `raw_api_responses` row, one row per exchange as before), the rest of the page is
  stored and the next page is requested. A round with a rejected record marks no source
  deletions; the next round without one does. Retrieval receipts keep the number of rejected
  records apart from `unplaced_count`. `/health` reports `degraded` with
  `rejected_google_records` until the maintainer runs `health-mirror rejections --ack <id>`; a
  record rejected again later stays acknowledged, a new one turns it `degraded` again
  ([docs/RUNBOOK.md](docs/RUNBOOK.md), “jsonb'nin tutamadığı Google kayıtları”).
- Sleep summaries (`respiratory-rate-sleep-summary`) are no longer written onto one another.
  Google returns every such record under one name whose data point id is empty
  (`.../dataPoints/`), so each sleep overwrote the last and the archive held one row whose
  earlier sleeps were only versions. A record with such a hollow name is now identified by the
  name and its own time (the end of the sleep it describes): one row per sleep, never per
  calendar day, so sleep by day, sleep across midnight and two sleeps of one day are each a
  record. Coverage and Umay date them, like sleep, by the local day the sleep ended. The old row
  and its versions are kept unchanged; once the row of its own sleep holds the same payload,
  an append-only `hollow-name` supersession hides it, so the sleep counts once. No migration;
  the next complete fetch of a window (`health-mirror reconcile --days N`) stores every sleep
  Google still returns as its own row ([docs/REFERENCE.md](docs/REFERENCE.md#hollow-google-names)).

### Changed

- Durations under one hour read “45 dk” instead of “0 sa 45 dk” in the workout duration, the
  sleep card, the last-7-days table and the metric history.
- Clearer interface text in the appearance dialog (System › Appearance › Özelleştir): a shorter
  intro that says the presets are shared across devices, Umay presets without the repeated
  “Umay ·” prefix, “Klasik”, consistent hints and error messages; “Beden modeli otomatik dönsün”
  in the panel; a typo and an English leftover fixed in the coverage reasons.
- README screenshots retaken from the current interface with synthetic preview data: desktop and
  mobile dashboard, plus new images of Umay's chat (desktop and mobile), the metric history, the
  date picker and the appearance dialog.

### Added

- **Apple Watch / Apple Health, two gates (GitHub version only).** Upload the Health app's
  `export.zip` (or `export.xml`) on the System page ("Apple Sağlık içe aktar") or with
  `health-mirror apple-import PATH`: streamed, constant memory, multi-gigabyte files work, a
  re-upload adds only what is missing. Or let the Health Auto Export iOS app post to
  `POST /webhooks/apple-health`: off by default (`APPLE_RECEIVER_ENABLED`), bearer token from a
  file (`health-mirror apple-token`), optional https host admitted for that path only
  (`APPLE_RECEIVER_ENDPOINT_URI`), body and request limits. Records are archived first
  (append-only), then normalized with `provider = 'APPLE'` and source code `HEALTH_KIT`, so the
  precedence policy, the dashboard, the workout list, **Cihazlarım** and Umay see them; Watch and
  iPhone samples and the two gates are never added together. HRV from Apple is SDNN and is kept
  apart from RMSSD. The receiver follows the app's published format; its fixtures are synthetic
  ([docs/REFERENCE.md](docs/REFERENCE.md#apple-health-github-version)).

- Source precedence (the maintainer's decision of 7 November 2026): when several devices report
  the same data, sport activities show Garmin first and every other health metric shows the
  Fitbit first; the other sources stay visible as alternatives and nothing is summed. The order
  per category is configuration (`SOURCE_PRECEDENCE_ACTIVITY=GARMIN,FITBIT,HEALTH_KIT`,
  `SOURCE_PRECEDENCE_HEALTH=FITBIT,GARMIN,HEALTH_KIT`; reverse a line to reverse the rule). The
  dashboard's day values, hourly heart chart, Umay's `daily` and `devices`, the workouts list and
  `GET /api/garmin/daily` go through the resolver (`app/services/source_precedence.py`) and return
  `source`/`alternatives`; the interface shows a text badge for the primary source, the secondary
  sources in a collapsed row or "diğer kaynaklar", and Cihazlarım says per device in which
  category it is primary ([docs/REFERENCE.md](docs/REFERENCE.md#source-precedence)).
- `garmin-backfill` normalizes every archived activity that begins in its `--since`/`--until`
  window, whenever it was first archived: an archive filled before normalization existed gets
  its activities into `exercise_sessions` from one backfill over their window (the periodic pull
  still normalizes only what it archived itself).
- Garmin phase 2: the Garmin raw archive is normalized into the shared normalized tables tagged
  `provider = 'GARMIN'` after every pull and backfill (steps and distance, heart-rate samples,
  resting heart rate, sleep, HRV, SpO₂, stress and body battery as scalar metrics, activities as
  exercise sessions), idempotently (unchanged rows are not written). Google's readers never see
  these rows (they join through `raw_health_data`), deletion marking never marks them, and the
  Cihazlarım panel lists the Garmin source (`GARMIN_DEVICE_NAME`) with its last pull, newest data
  and precedence ([docs/REFERENCE.md](docs/REFERENCE.md#garmin-connect-personal-api)).
- Cihazlarım (System): every device the archive has seen, one row per device, with its
  platform, a status in words and colour (`bagli` for a newest record within 48 hours,
  `gecikmeli` up to 7 days, `kopuk` beyond; `DEVICES_CONNECTED_HOURS`, `DEVICES_DELAYED_DAYS`),
  the age of its newest record with the exact time in the chosen zone, the number of its data
  types and an accessible details dialog (data types, first and last sighting, previous names).
  A device renamed by Google ("Google Fitbit Air" → "Fitbit Air") is one row, joined by the live
  source chain. Served by `GET /api/devices`, read-only from `raw_source_sightings` and
  `raw_source_chains`; no migration. Google's paired devices add "Google son eşitleme" when the
  call succeeds and never block the list when it fails (`google: unavailable`). Umay's
  `health_archive` gains the `devices` action
  ([docs/REFERENCE.md](docs/REFERENCE.md#devices)).
- Garmin Connect Personal API as a second, read-only source (maintainer's decision of
  7 November 2026; off by default). With `GARMIN_ENABLED=true` and the Personal API key in
  `GARMIN_API_KEY_FILE` (never in `.env`, the database or a log), the scheduler job `garmin-pull`
  fetches the last `GARMIN_LOOKBACK_DAYS` every `GARMIN_PULL_INTERVAL_MINUTES` outside the
  02:00–03:59 UTC window: daily summaries, steps, stress, body battery, sleep (per day and
  summaries), heart rate, SpO₂, HRV (per day and summaries) and activity summaries. Every HTTP
  exchange is kept byte for byte in `garmin_api_responses` and every distinct record payload in
  `garmin_raw_records` (append-only; a changed day is a new row, the earlier row stays).
  `health-mirror garmin-status`, `garmin-pull` and `garmin-backfill --since … [--until] [--dry-run]`
  (chunked by the endpoints' limits, checkpoint per kind, resumes); `GET /api/garmin/status` and
  `GET /api/garmin/daily?from&to`. Garmin data is not normalized into the Google tables or shown
  on the dashboard in this phase ([docs/REFERENCE.md](docs/REFERENCE.md#garmin-connect-personal-api)
  has the dependency map and the phase 2 options). No new dependency: a thin `httpx` client.
- Coverage says why a day has no value. Umay's `coverage` card (schema 2) reads the retrieval
  receipts: per metric it counts closed days by retrieval state and splits the days without a
  value into `empty_response` (Google returned nothing), `returned_without_value`,
  `not_retrieved` (never requested since receipts began), `incomplete` and `unknown`; narrowed
  to one metric it lists the gap periods. `GET /api/sync/coverage` and the Kapsama panel gain
  `not_attempted` (“Hiç çekilmedi”) and `receipts_since`. Older history stays `unknown`; no
  migration ([docs/RUNBOOK.md](docs/RUNBOOK.md), “Kapsama ve çekim fişleri”).
- The time zone choice and the body model rotation under System › Appearance (Görünüm) are
  shared across devices: they are read from and written to `GET`/`PUT /api/settings/display`
  (migration `20261102_0022`). The record carries a `schema_version`; a row written by a newer
  version is neither misread nor overwritten (409). The browser's `localStorage` stays an offline
  copy: the first paint does not wait for the server, and a change made while the server is
  unreachable applies in that browser and is sent on the next contact.

## [0.2.1] - 2026-10-31

Covers everything merged since 0.2, mainly two integration batches: the Umay interpretation
layer with the split of large modules and a series of reliability fixes, and Umay persona v7.

### Upgrade notes

- Database migrations `20261024_0013` to `20261031_0021` run automatically when the app starts
  (`alembic upgrade head`). They are additive; no record is deleted or rewritten. Per-revision
  notes are in [docs/RUNBOOK.md](docs/RUNBOOK.md).
- `20261031_0021` needs a short `ACCESS EXCLUSIVE` lock on the two webhook tables and cannot run
  while a `pg_dump` is in progress. Update outside the nightly backup window (around 03:30); if
  they overlap, the migration stops after about 30 seconds without changing anything and succeeds
  on the next start ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
- Take a backup before updating, as always. Downgrades refuse when they would lose evidence
  written by the new revisions; roll back with a restore instead.
- Existing deployments: create the backup status directory (`install -d -m 755 backup-status`,
  or the `BACKUP_STATUS_HOST_DIR` you use) before `docker compose up`, so the backup account owns
  it. The backup script now needs Python 3.11+ on the host (standard library only).
- The PostgreSQL container now runs with a read-only root filesystem and tmpfs mounts for
  `/tmp` and `/var/run/postgresql`; data stays in the `postgres_data` volume.

### Security

- A signed webhook body that PostgreSQL `jsonb` cannot hold (`\u0000`, unpaired surrogates,
  `NaN`/`Infinity`, numbers that overflow) is no longer lost: the ingest used to roll back and
  the signed bytes were never stored. The body is now kept byte for byte in `raw_body` with
  `raw_json` left NULL, the affected item is rejected, and clean items in the same delivery are
  still processed. Existing rows are untouched (migration `20261031_0021`).
- Unauthorized webhook requests no longer use up the budget of authorized Google deliveries:
  authorized and unauthorized requests have separate rate budgets, a missing key set answers 503
  instead of 401, and pre-authorization rejections write at most one diagnostic row per reason per
  minute.
- Webhook and manual-entry endpoints enforce body size and rate limits
  (`MANUAL_WRITE_MAX_BODY_BYTES`, `MANUAL_WRITE_REQUESTS_PER_MINUTE`,
  `WEBHOOK_REQUESTS_PER_MINUTE`).
- OAuth token handling is stricter: redirects, HTTP 206, error envelopes, duplicate JSON keys,
  non-JSON numeric constants, invalid refresh-token types and access tokens that cannot travel in
  an HTTP header are refused instead of being cached or activated; token responses are read with
  memory limits.

### Added

- Umay interpretation layer: tool-based interpretation, a per-turn view of the health data sent
  to Umay, a source-separated derived daily data foundation with a 64 KiB budget per daily card,
  and a coverage action for `health_archive`.
- Umay persona v7: voice-focused persona, no scripted opening lines, reasoning effort `high`, and
  the real local date and time in every turn.
- Coverage report built on append-only retrieval receipts, with an API (`GET /api/sync/coverage`)
  and a panel under System › Coverage (migration `20261025_0015`).
- Durable reconciliation run reports, linked to the watchdog (`GET /api/sync/reconciliation`,
  migration `20261025_0016`).
- Webhook diagnostics in the service, API and panel, kept immutable in the database (migrations
  `20261025_0017`, `20261025_0019`).
- Test-entry flags on manual measurements as append-only events; a flagged entry is left out of
  the profile, the 3D body and Umay's daily data (migration `20261024_0014`).
- Bounded retry for failed derived days (`python -m app.cli.derived_retry`).
- Backup status exposed through the API (`GET /health/backup`) and a completed restore
  comparison; a restored copy without tables now fails the drill.
- Monthly timeline in the sports view; keyboard and accessibility behavior of the single-screen
  interface; an enforced first-load budget, with the body viewer loaded when it becomes visible.
- Linux service lifecycle for the Umay bridge (`deploy/systemd/healthos-bridge.service`).

### Changed

- Every analytics reader counts the same set of records (migration `20261024_0013`).
- Versioned local daily read models with transactional invalidation (migrations
  `20261025_0018`, `20261025_0020`).
- Large modules were split into smaller ones with unchanged behavior: identity resolution and
  backfill, source chains, raw history, identity models, the CLI, the body view and the
  stylesheet.
- Sleep totals: invalid sessions no longer count as a measured zero, restless time is subtracted
  and assumptions are shown.

### Fixed

- Google sync no longer reports a false completion or hangs on malformed responses: error
  objects, wrong or non-object collection items, duplicate JSON keys, non-JSON numeric constants,
  HTTP 206 and redirects, malformed page tokens, endless pagination, stalled bodies and
  `Retry-After` headers, truncated compressed bodies and trailing bytes after a deflate stream.
  Bytes already received are kept in the archive when a request fails or is cancelled.
- Memory use is bounded while reading and decompressing Google and OAuth responses.
- A malformed field (dates, numbers, heart-rate or text metadata, exercise type) no longer rolls
  back a whole retrieval page.
- Invalid step, sleep, heart-rate, weight and exercise corrections no longer bring back the old
  value.
- Backfill jobs and same-type syncs cannot run twice across processes; the watchdog recovery lock
  no longer blocks the database pool; live webhook work is not reclaimed during recovery.
- Webhook date boundaries no longer cause an endless retry loop, and broken webhook metadata no
  longer rolls back valid notifications.
- Numeric overflows in dashboard calculations and Umay's derived data are handled; dashboard date
  windows that overflow answer 422.
- Quadratic scans in Umay's calculations and sleep summaries were removed and their output is
  bounded.
- Backups made in the same second or concurrently no longer overwrite each other.
- The grain texture and the background chosen under System › Appearance show on the home page
  again.

## [0.2] - 2026-10-23

The first release under the current numbering (tag `v0.2`). Earlier snapshots were labelled
0.5.0 and are not described here.

### Added

- A self-hosted, read-only archive of the Google Health API v4: OAuth with read-only scopes,
  resumable backfills by data type and time window, overlapping incremental sync, signed
  webhooks that trigger retrieval, daily reconciliation and persisted checkpoints.
- Raw-first storage: every Google response kept byte for byte, every record's payload kept with
  all of its earlier versions, and database triggers that refuse deletes and rewrites;
  `verify` checks them (migrations `20261020_0010` and `20261020_0011`).
- Record identity for records without a Google id, so a renamed device or a reshaped interval is
  the same record; source chains decided by a nightly reconciler or by the owner, and
  duplicates superseded instead of deleted (migration `20261021_0012`).
- Source deletions are marked with `source_deleted_at` and an event; no row is deleted.
- A single-screen Turkish dashboard with daily cards, hourly charts, a 7-day history, CSV
  export, manual body measurements and workouts, a 3D body reference and an appearance panel
  whose choices are shared across devices.
- Umay, an optional chat companion through a local Codex bridge, with read-only archive tools.
- Encrypted, scheduled backups that are kept forever, with a free-space guard and a restore
  drill.

### Security

- The interface and API sit behind a single-user access token with server-side login
  sessions; security headers and a CSRF guard on manual entries; no interactive API
  documentation in production; the Umay bridge never listens on all interfaces.

### Changed

- Operations hardening: a production guard in Compose, log rotation, a scheduler misfire
  allowance, recovery of work left running at start-up, and backfill windows that keep failing
  are set aside after ten attempts instead of blocking the rest.
- CI runs PostgreSQL integration tests, browser checks of the single-screen rule and pyright;
  the documentation was reorganized into a deployment guide and a runbook.
