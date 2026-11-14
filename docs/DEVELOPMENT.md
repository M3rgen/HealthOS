# Development

Run commands from the repository root unless indicated otherwise. Python dependencies are locked with `uv`; frontend dependencies use `npm ci`.

## Native setup

Install Python 3.12 or 3.13, uv 0.13.6, PostgreSQL, and Node.js 24. Copy `.env.example` to `.env` and configure a private development database:

```dotenv
DATABASE_URL=postgresql+asyncpg://YOUR_USER:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/YOUR_DATABASE
```

Replace placeholders locally. Keep the development database separate from your real archive.

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic upgrade head
```

Build the frontend:

```sh
cd frontend
npm ci
npm run build
cd ..
```

Start the application:

```sh
uv run --frozen --extra dev uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

For frontend hot reload, run `npm run dev` from `frontend` while the backend is running. Vite proxies API requests to the local backend. Google credentials are optional for development; most tests create synthetic records in an in-memory database.

See [local UI authentication](UI_AUTH.md) for tokenless loopback access, Secure cookies and explicit proxy trust.

## Validation

`make verify` runs lint, format check, `scripts/verify_configuration.py`, the unit suite (pytest, parallel) and `pyright app`, in that order, and stops at the first failure. It needs no database or network. Everything CI runs:

| Command | What it runs |
| --- | --- |
| `make verify` | `ruff check`, `ruff format --check`, `scripts/verify_configuration.py`, `pytest -q -n auto`, `pyright app` |
| `make test` | The unit suite alone (`pytest -q -n auto`); `pytest -q -p no:xdist` runs it in one process |
| `make coverage` | Unit suite with line and branch coverage of `app/`; fails below `fail_under` (92.6, `pyproject.toml`) |
| `make coverage-security` | `coverage`, plus a 90 % floor on the authentication, credential, login and webhook-verification modules |
| `make test-postgres DATABASE_URL=postgresql+asyncpg://...` | `tests/integration`, then the whole unit suite against a migrated PostgreSQL (`HEALTHOS_TEST_BACKEND=postgres`). Needs a disposable server and a role with `CREATEDB`; fails, does not skip, without `DATABASE_URL`. Tests that describe SQLite itself are named in `tests/support/pg_backend.py` |
| `make test-clock` | Unit suite at eleven clock settings that break date arithmetic and on non-UTC hosts (`tests/support/clock_matrix.sh`; slow). `time-machine` is not a project dependency: each run installs it into a temporary directory, so it needs network or a populated `uv` cache |
| `make test-clock-preflight` | Under a minute: proves only that the clock harness can shift time and zone. Runs on every change in the `verify` job, so a broken harness fails a blocking job instead of the weekly `clock.yml` |
| `make compose-smoke` | `scripts/verify_compose.py`: builds and drills the production Compose file in its own temporary project and removes only that project. Under Colima, run it from a directory inside your home folder; elsewhere it stops at once with an error naming the path |
| `make test-backup-unit` | `scripts/verify_backup_unit.sh`: the shipped `healthos-backup.service` under real systemd, against a disposable PostgreSQL, with the ownership the deployment guide leaves behind. Needs systemd as PID 1, passwordless `sudo`, Docker with Compose, `python3`, `age` and `age-keygen` (on macOS, a running Colima VM). Exit 2 means this host cannot run it |
| `make test-browser` | `cd frontend && npm run test:browser`: type check, unit, build-budget and Playwright checks |
| `make test-e2e` | `tests/e2e/run_journey.py`: the one check without fixtures. Builds the frontend into `app/static`, seeds a synthetic months-long archive, starts the real app in production mode and drives Chromium through login, the panel, a measurement written through the API and a reload, under the one-screen rule, then reads the value back from PostgreSQL. Needs `DATABASE_URL=postgresql+asyncpg://user:password@127.0.0.1/...` to a disposable local server whose role can create databases; the URL must carry a password (a local `trust` server accepts any) and a non-local host is refused. Also needs `npm ci` in `frontend` and Playwright's Chromium. It creates and drops only its own database and never reads `.env`. Fails, does not skip, without `DATABASE_URL` |
| `make test-order [SEED=n]` | The unit suite in a random order (`tests/support/shuffled_order.py`); prints the seed, and `SEED=` repeats a run. `clock.yml` runs it weekly |
| `cd frontend && npm run lint` | ESLint (`no-unsanitized`) and the `checkJs` type check |
| `uv lock --check` | The lock matches `pyproject.toml` (separate from `make verify`; CI runs it) |

Every test has a 60-second timeout (`pytest-timeout`, `pyproject.toml`), so a hang fails instead of stalling the run. Tests that need a real loopback listener use the `local_sockets` fixture: it skips only where the environment refuses `bind`, and with `CI` set or `HEALTHOS_REQUIRE_SOCKETS=1` a refused bind fails. PostgreSQL tests skip unless `DATABASE_URL` points at PostgreSQL (each creates and drops its own `healthos_it_*` database); `HEALTHOS_REQUIRE_POSTGRES=1` turns the skip into a failure. The backup round-trip test also needs PostgreSQL client tools: `HEALTHOS_PG_DOCKER` names a container on the same server, or `HEALTHOS_PG_BIN` a directory of binaries; without either it skips, and `HEALTHOS_REQUIRE_PG_CLIENT=1` makes that a failure (CI sets it).

**Limits checked in tests and lint:** ESLint keeps frontend functions to 60 lines and complexity 10 (at most 5 parameters; files to 400 lines; lines to 180); `frontend/tsconfig.json` is fully `strict`, and `npm run lint` (and `npm run test:browser`) also run `frontend/scripts/check-strict.mjs`, a `tsc --strict` ratchet that allows no file more errors than `frontend/strict-baseline.json`; that baseline is empty, so no file may have a strict-mode error. Pyright `standard` mode on `app/`; Ruff `C901` complexity at most 16 (`max-complexity`, `pyproject.toml`); `tests/test_function_budgets.py` keeps every production function under 150 lines with at most two over 100; `tests/test_import_cycles.py` and `tests/test_private_imports.py` guard the import graph and the number of imports of private names. Split code rather than raising a limit.

Browser checks in `tests/browser` use Playwright's bundled Chromium (`cd frontend && npx playwright install chromium` once) and answer every backend route with synthetic fixtures. `npm run test:browser` builds the frontend into `frontend/.test-output/dist`, serves it with `vite preview` on a free port, runs each check and prints pass/fail/skip; `-- --dev` uses the Vite dev server instead, `-- --only job-csrf,appearance` runs a subset, and `HEALTHOS_TEST_URL=<url>` targets a server you started (a single check also runs as `HEALTHOS_TEST_URL=<url> node tests/browser/<name>.cjs`; default `http://127.0.0.1:5174`). The harness points the Vite proxy at a stub that answers 503, so a running backend and its archive are never reached. Screenshots go to `frontend/.test-output/` (gitignored). `umay-chat.cjs` replaces the chat socket in the page; set `UMAY_BASE_URL` to run it against the app served by the backend instead, and it is skipped when nothing answers there. Never use a real archive for public screenshots. Validate desktop and mobile dimensions after interface changes: the main document must not scroll, and long content must remain reachable inside its panel.

**Fixture contract.** The browser checks trust the hand-written `frontend/preview/fixtures.cjs`, so `tests/test_preview_fixture_contract.py` (part of `make verify`; needs Node) runs the real FastAPI routes over a synthetic archive and compares each response with the fixture served for that route. A new field in a real response fails until someone decides: add it to the fixture, or, if the interface does not read it, to `NOT_IN_FIXTURE` (or `NOT_IN_FIXTURE_PREFIXES`) in that test, whose companion test fails if `frontend/src` does read a listed field.

## Continuous integration and releases

The public GitHub snapshot includes the application, migrations, the installers, deterministic dependency locks, automated tests and CI. Google access and available records vary by project and account.

`.github/workflows/ci.yml` runs on pull requests and on pushes to `main`. Its fourteen gates: `verify` (lock, `make test-clock-preflight`, lint, `make coverage-security`, `make test-postgres` with appended coverage, `make ci-coverage-report` combining SQLite and PostgreSQL at the unchanged floors, migrations and `alembic check`, configuration policy, Pyright, Compose model; Python 3.12 is the gate and 3.13 is informational), `bridge_boundary` (scripted CLI subprocess and JSON-RPC boundary), `installer`, the guided installer's own gates `installer_lint` (shellcheck and PSScriptAnalyzer), `installer_hosts` (installer tests on macOS and Windows), `installer_linux_docker` (real Docker installs, trial and full mode without Google, then a real encrypted backup) and `installer_windows_ps51` (every `.ps1` parsed by Windows PowerShell 5.1, a dry run in trial and full mode), `browser` (ESLint, Vite build with budgets, browser checks), `e2e` (`make test-e2e` against a PostgreSQL 17 service), `security` (pip-audit over locked runtime dependencies, `npm audit --omit=dev --audit-level=high`, image build and a reproducible second build, Trivy HIGH/CRITICAL fixable), `workflows` (actionlint 1.7.12 and the runner's ShellCheck), `secrets` (Gitleaks over all history), `codeql` (Python and JavaScript; `.github/scripts/codeql_gate.py` fails on severity 7.0 or above and error level, and upload alone never fails a job) and `smoke` (`make compose-smoke`, a real backup and restore, the image's file tree kept as the `smoke-image-manifest` artifact for `release.yml`, and `make test-backup-unit`). `summary` passes only when all fourteen pass; `tests/test_ci_gates.py` fails if a gate is removed or switched off. A finding judged acceptable goes in `.github/codeql-exceptions.json` with a reason; the file starts empty and leaving it empty is a maintainer decision.

Scheduled workflows re-check code that has not changed: `audit.yml` runs the same dependency and image audits daily (06:17 UTC), `clock.yml` runs the unit suite at eleven clock settings weekly (Mondays 05:41 UTC, `tests/support/clock_matrix.sh`), and `codeql.yml` re-scans weekly (Mondays 04:23 UTC). CI and the shipped image both pin Python 3.12.16; the pinned `uv` 0.13.6 supports installing that exact patch. `make compose-smoke` is what exercises the image itself.

`npm audit` has two levels. The gate, in `ci.yml` and `audit.yml`, is `npm audit --omit=dev --audit-level=high`: the runtime dependencies that ship to the browser. The full tree, build tooling included, runs in `audit.yml` alone and fails that scheduled audit on a high or critical advisory. The build tooling is not in the image, but Vite builds the bundle that ships, so an advisory in its tree can still reach users. A previous audit found six advisories, two high, in Vite's tree and none in the production tree; the Vite upgrade to 8.3.8 closed the two high ones (see the [changelog](../CHANGELOG.md)).

This publication guard is only for the maintainer clone that publishes `github-main` to GitHub.
Install the local publication guard with `make hooks` (sets this clone's `core.hooksPath` to
`scripts/hooks`; replaces any existing hook path). Pushes to `origin` accept only
`github-main:main` or a release tag `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH.N` (interim, N ≥ 1) that matches the project version and is on
`github-main`. Both need a receipt from `make ci-gate` (runs `.github/workflows/ci.yml` for that
exact commit under `act` and Docker) and require `gitleaks git` to pass for the outgoing range; a
new remote branch or tag scans the entire reachable history. Install Gitleaks locally before
publishing. Repository rulesets, CODEOWNERS and the release rules are in
[Continuous integration and releases](#continuous-integration-and-releases); the admin apply commands are in [`.github/rulesets/`](../.github/rulesets/). The snapshot
script accepts repeatable local `healthos.githubForbidPattern` values (case-insensitive
extended regular expressions) for the maintainer's own markers such as account names, home paths
and host names; they are never written into the repository. When none is set, only the built-in
generic private-network patterns apply. It checks
contents and paths, and runs `gitleaks dir` with `--dry-run`.
The snapshot also rejects data/export extensions, FIT, archive, database, OpenPGP/age,
Apple Health and Parquet signatures, and JSON files over 100,000 bytes. It reads the final
Git tree's blobs, reports only paths, reasons and byte counts, and stops even with
`--dry-run` or `--allow-references`. A synthetic fixture may pass only after review adds
its exact path, SHA-256 and provenance to `DATA_FILE_ALLOWLIST` in
`scripts/github_snapshot_checks.py`; changed bytes require a new review. Local
`healthos.githubDataJsonLimit` accepts only 1–100000 bytes and repeatable
`healthos.githubDataForbidExtension` values add forbidden extensions. Local settings
cannot add allowances or relax the built-in rules.
Its default commit message is the first `##` version heading and its complete body from the
publication tree's `CHANGELOG.md`, preserving subheadings and bullet lists without the `##`
prefix. Explicit messages also reject private branch names and hexadecimal words of 7–64 digits.
`make ci-gate [REV=<rev>]` runs `ci.yml` for that exact commit under `act` and Docker and records the receipt in the common Git directory (`healthos-ci-gate/<commit>`), only when every job passes (Python 3.12 only; check 3.13 with `act push -j verify --matrix python:3.13`). `workflows` runs under `act` on both x64 and arm64 using SHA-256-verified release archives and the runner's ShellCheck; its new job has not yet been exercised under `act`. `codeql`, `smoke` and the four installer gates cannot run under `act` (no Linux arm64 CodeQL; bind mounts resolve on the Docker host, not in the `act` container; no macOS or Windows runner; PSScriptAnalyzer comes from the PowerShell Gallery): they skip themselves and the receipt ends with `not-run-under-act codeql smoke installer_lint installer_hosts installer_linux_docker installer_windows_ps51`, so a receipt never claims them. That also skips `make test-backup-unit` and the manifest step. Run `make compose-smoke` and `make test-backup-unit` yourself. `act` runs on the Docker host's architecture, so on an Apple Silicon Mac a receipt is an `arm64` result while GitHub-hosted runners are `x64`.

The two files in [`.github/rulesets/`](../.github/rulesets/) (required `summary` check and code-owner review on `main`; maintainer-only immutable `v*` tags) and [`.github/CODEOWNERS`](../.github/CODEOWNERS) are applied by a repository admin with the commands in that README. `release.yml` refuses a tag unless it is well-formed, matches `pyproject.toml`, is an ancestor of `origin/main` and that exact commit has a successful `ci.yml` on `main`; it then builds, refuses an image whose file tree differs (timestamps aside) from the one that commit's `smoke` job tested, scans, generates an SBOM, attests provenance and attaches the image archive. Do not create or push a tag outside this flow.

**Verification status, 14 November 2026:** `ci.yml` and `release.yml` ran on GitHub Actions for v0.2.2.1: the `release` environment exists with the maintainer as required reviewer, and the gate, build, attestation, provenance verification and publish jobs succeeded. The rulesets have not been applied. After a release, anyone can verify its provenance with `gh attestation verify` ([rulesets README](../.github/rulesets/README.md#verifying-a-release)).

The `bridge_boundary` job runs `tests/test_codex_probe_decisions.py` and
`tests/test_codex_runtime_rpc.py`. The existing Python peer in `tests/support/probe_cli.py`
returns `--version` and bundled model metadata, exchanges initialize/config/thread/turn JSON-RPC
messages over real subprocess stdio, and scripts safe and unsafe outcomes. These tests check
attestation decisions, image/sub-agent rejection and callback ownership without an account or
model. They cannot prove OS sandbox enforcement by the real CLI. No real-CLI test is removed or
reclassified: `test_codex_bridge_runtime.py` and `test_codex_bridge_front_runtime.py` still require
an explicit `HEALTHOS_CODEX_CLI` absolute path and do not run against the stub.

### Release versions

Interim releases use `0.2.2.N` (tag `v0.2.2.N`); reaching the target advances the patch
version to `0.2.3`. The tag without `v` must equal the `pyproject.toml` version; the pre-push
hook and `release_gate` enforce that equality. Frontend manifests use `X.Y.Z+N` because npm
does not accept four release components; a target release uses plain `X.Y.Z`.

Before publishing, manually date the first CHANGELOG section (`YYYY-MM-DD`) when tagging.
After the release, run `make bump-interim` to advance the counter, or
`make bump-target VERSION=0.2.3` for a greater three-part target. Both update the five release
files and add an Unreleased section with a placeholder bullet; an existing Unreleased section
blocks the command without changing any file. Only the healthos version line in `uv.lock` is
edited directly, without running `uv lock` or resolving dependencies.
With the guided installers in the tree, the same commands also move `HEALTHOS_INSTALLER_VERSION`
in `install.sh` and `install.ps1` and the release tag (`v<version>`) their install URLs and
`git clone --branch` lines pin in `README.md`, `docs/INSTALL.md`, `docs/INSTALL.tr.md` and
`docs/AI_INSTALL.md`; a link to an already published release is left alone. A marker that does
not match the project version stops the command before any file changes.

`scripts/github_snapshot.sh` never changes the version; it uses the first CHANGELOG section
as the snapshot commit message. `tests/test_version_agreement.py` checks all five locations:
pyproject, uv.lock, package.json, package-lock (both fields), and the first CHANGELOG heading.

### Release approval and credentials

Before enabling publication, an administrator must create the `release` environment with an
independent required reviewer, prevent self-review and disable administrator bypass as described
in the [rulesets README](../.github/rulesets/README.md#the-release-environment). The `publish` job
already declares `environment: release`; this declaration alone does not configure protection.
Both checked-in rulesets forbid bypass. The snapshot script and hook must first support a reviewed
branch and pull request; the old direct push will fail when the main ruleset is applied.

| Name | Source and use |
| --- | --- |
| `GITHUB_TOKEN` | GitHub's per-job token (`github.token`), passed to `gh` as `GH_TOKEN`; reads CI/artifacts and attestations, and has `contents: write` only in the reviewed `publish` job |
| `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_URL` | GitHub-provided OIDC credentials for the `attest` job with `id-token: write`; consumed by the provenance Action |

No manually stored repository/environment secret or personal access token is required by this
workflow. Do not store token values in the repository. Repository/environment configuration and
the first hosted approval and provenance check still require maintainer verification.

## Repository map

| Path | Responsibility |
| --- | --- |
| `app/api` | Local HTTP routes and Codex chat. |
| `app/google_health` | OAuth, pagination, data types, and webhook verification. |
| `app/sync` | Backfill, incremental sync, checkpoints, and reconciliation. |
| `app/services` | Storage, dashboard read models, scheduling, and optional AI tools. |
| `app/models` / `migrations` | Database schema and versioned migrations. |
| `frontend` | Dashboard interactions and the Three.js body reference. `frontend/public/assets/boot-guard.js` is a plain script outside the bundle, served from `/assets`, because the Content-Security-Policy blocks inline handlers: if a part of the bundle cannot load (an old tab after an update), it shows an error screen with a reload button. |
| `tests` | Unit, integration, and browser checks; `tests/support` holds the fixtures shared across them. |
| `scripts` | Operations, configuration verification, and bridge launcher. |

The installed CLI and the Python distribution are both named `healthos`. The layers and their import rules are described in [Architecture](ARCHITECTURE.md).
