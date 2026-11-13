# Architecture

How HealthOS is put together, which way the code may depend on itself, and why a few parts are
larger than a single-user application would seem to need. The operational detail (every
setting, table and command) is in the [technical reference](REFERENCE.md); this page is the map.

## Principles

1. **Raw first, normalize second, analyze later.** What a source sent is stored before anything
   is derived from it, and it is never deleted or overwritten. Database triggers enforce this
   for every code path, including the CLI and hand-written SQL (`healthos verify` checks that
   they are in place). Normalized tables, read models and views can always be rebuilt from the
   raw archive; the raw archive cannot be rebuilt from them.
2. **Read-only towards every source.** HealthOS only reads from Google and Garmin. A deletion
   reported by a source is recorded as a mark (`source_deleted_at`, an event row), never as a
   deleted row.
3. **One process, one database.** A single FastAPI process serves the interface and the API
   and runs the scheduled jobs (APScheduler) in the same event loop. PostgreSQL holds the
   archive, the synchronization state and the read models. There is no queue, cache server or
   second service; the optional Umay bridge is a separate process on the host.
4. **Private by construction.** The interface binds to `127.0.0.1`; only the Google webhook
   route is meant to be reachable from outside ([Privacy](PRIVACY.md)).

## Layers

The backend is the `app` package. Each subpackage is one layer:

| Layer | Package | Responsibility | May import |
| --- | --- | --- | --- |
| Composition | `app/main.py`, `app/runtime.py` | Builds the FastAPI app: middleware, routers, the runtime (which constructs the Google and Garmin engines) and the scheduler. | everything below |
| Interface | `app/api` | HTTP routes: parse the request, call a service, shape the response. | `services`, `schemas`, `core`, `google_health` and `garmin` client error types and data-type lists, the `db` session dependency; `runtime` for injected engines |
| Interface | `app/cli` | The `healthos` command line (Typer). | `services`, `runtime` (builds the engines), `db`, `core`; no `models`, and it builds no query of its own |
| Application | `app/services` (subpackages below) | Everything the product does: storing pages, identity, readers, dashboard, coverage, precedence, Umay's tools, backups status, scheduling, the `IngestionSource` contract between sources and engines. | `parsers`, `models`, `schemas`, `db`, `google_health`, `garmin`, `apple`, `core` |
| Retrieval | `app/sync` | The Google retrieval engines: incremental sync, backfill, reconciliation, checkpoints, deletion marking (through `app/services/archive/source_deletions.py`), the pipeline watchdog and webhook processing. Incremental sync and backfill share one fetch/store/receipt cycle (`app/sync/page_cycle.py`). | `services` (storage, receipt, report and pipeline operations), `models`, `db`, `google_health`, `core` |
| Parsing | `app/parsers` | Return record kinds, normalized values and v2 identities; text helpers also inspect ORM classes supplied as arguments. | `core`; no `models` imports |
| Clients | `app/google_health`, `app/garmin` | HTTP clients, OAuth, pagination, response decoding, webhook signature checks. No database access. | `core` |
| Apple parsing (published release) | `app/apple` | The GitHub-only Apple extension's streaming `export.zip`/`export.xml` reader and Health Auto Export parser. No database access. | `core`, `google_health` (its strict JSON reader) |
| Projections | `app/schemas` | Shared TypedDict response shapes used by services and API routes. | no other `app` package |
| Schema | `app/models` | SQLAlchemy tables, the trigger and guard definitions, shared query fragments (what a reader counts). | `db`, `core` |
| Database | `app/db` | Engine, session factory and the database adapter (`app/db/dialect.py`). Production installs only the PostgreSQL adapter; test operation replacements live in `tests/support/`. SQLite DDL, guards and the test ownership lock live in `tests/support/sqlite_*.py`. | `core` |
| Foundation | `app/core` | Settings, authentication, logging, time and provenance helpers, request limits. | no other `app` package (ASGI middleware uses Starlette) |

The application layer has explicit module interfaces in each subpackage's `__init__.py`
(`__all__`). Modules load on demand so importing a contract or the host backup helper does
not initialize unrelated services or the database stack. All implementation modules
are retained; identity and source-chain modules remain separate.

| Service package | Responsibility |
| --- | --- |
| `app.services.archive` | Raw pages, versions, response evidence, normalization, source-deletion marks and manual-measurement flags |
| `app.services.identities` | Identity resolution, backfill, verification, source chains, precedence, analytics views and deferred deletions |
| `app.services.providers` | Google/Garmin ingestion contracts, credentials, pulling and FIT import |
| `app.services.dashboard_views` | Dashboard pages, devices and history built on `readers` |
| `app.services.readers` | Read models shared by the dashboard, interpretation, providers and coverage |
| `app.services.interpretation` | Canonical daily/sleep data, `archive_query`, derived models and bounded analysis |
| `app.services.umay` | Chat, health tools, transfer receipts and bridge policy/runtime |
| `app.services.synchronization` | Scheduling, jobs, coverage, retrieval evidence, webhooks and pipeline health |
| `app.services.operations` | Local settings, backup publication and backup status |
| `app.services.shared` | Finite arithmetic, civil query selections and pg_dump round-trip comparison |

Flat `app.services.<module>` imports are compatibility aliases deprecated since **0.2.2.1**;
remove them in **0.2.4**, after the 0.2.3 release. They refer to the same module objects, preserving
caller overrides and exception/type identity. Application imports use the new packages.
`app.services._compat` resolves aliases lazily from the package interfaces, without
duplicating the implementation files. Only the backup command retains a file shim.
The recursive layer guards scan their implementations and interfaces with the same
restrictions and exception counts. Path-specific budgets and dialect guards also point to
the implementations. The backup alias still supports its existing `python -m` invocation.

Schema changes live in `migrations/` (Alembic, a revision graph with one head; the
`20261107_0025` branchpoint is merged at `20261109_0029`). The interface is a separate
Vite project in `frontend/`; `vite build` writes it to `app/static`, which FastAPI serves. The
browser talks only to the app's own routes.

### Import direction

Most dependencies point down the table. API and CLI modules also import the concrete
`app.runtime.Runtime` composition type to access injected engines; this is an existing upward
edge, not a rule enforced by the table. The table records the implemented dependencies; the
guards below cover specific prohibitions rather than every possible edge:

- **`app/api` does not import `app.models`.** Most routes receive a session from `app.db.session`
  as a FastAPI dependency and delegate queries to services. The Umay WebSocket instead passes
  the module-global `SessionLocal` to its relay; OAuth linking and webhook acceptance also
  coordinate workflows in their routes and call runtime clients/engines. A guard test keeps `app/api` free of model
  imports, with an explicit allowlist for any exception. The CLI follows the same direction:
  it imports no `app.models` and `tests/test_database_boundaries.py` fails if it imports
  SQLAlchemy or runs a query on a session itself.
- **Clients never touch the database** (`app/google_health`, `app/garmin`), and `app/core`
  imports nothing else from `app`, so both can be tested without a database.
- **`app/models` knows nothing of services.** Shared read rules that every reader must apply
  the same way (for example `app/models/eligibility.py`, what a reader counts) live next to the
  schema so that the dashboard, the CSV export, Umay and the SQL views cannot drift apart.
- **`app/services` does not import `app/sync`.** The retrieval engines call the storage, receipt
  and report services to write a page; `app/runtime.py` constructs and starts the engines. Google
  and Garmin implement the same typed provider contract, the `IngestionSource` Protocol in
  `app/services/providers/ingestion_contracts.py` (fetch, store the raw page, normalize, report status),
  and a Pyright guard test keeps both conforming; `app/services/providers/ingestion.py` adds Google's page
  adapter. `app/db/dialect.py` dispatches database operations. The dialect-branch guard permits
  only `app/db/dialect.py`. SQLite trigger DDL and metadata listeners live in
  `tests/support/`; the test-only ownership path uses a local file lock. These are not PostgreSQL
  runtime support for SQLite, and the SQLite suite does not prove PostgreSQL lock/trigger
  semantics: `make test-postgres` is the separate gate. `tests/test_import_cycles.py` guards
  the import graph and `tests/test_private_imports.py` bounds imports of other modules' private names.

The enforced import restrictions include `api|cli → models`, `parsers → models`,
`services → api|sync|fastapi`, `models|google_health → services|sync|api` and `core → fastapi`.
Every layer's imports are checked against the May import column (`tests/test_api_boundaries.py`).

## Data flow

```mermaid
flowchart LR
    subgraph Sources
        G[Google Health API v4]
        GW[Google webhook]
        R[Garmin Connect Personal API]
    end
    subgraph Retrieval
        GS[app/sync engines<br/>incremental, backfill,<br/>reconciliation]
        RP[garmin_pull]
    end
    subgraph Raw archive, append-only
        GR[(raw_api_responses<br/>raw_health_data<br/>+ versions, identities)]
        RR[(garmin_api_responses<br/>garmin_raw_records)]
        WE[(webhook_events)]
    end
    subgraph Normalized
        N[(steps, heart_rate, sleep_sessions,<br/>exercise_sessions, ...<br/>provider = GOOGLE or GARMIN)]
        D[(derived_days,<br/>analytics_* views)]
    end
    G -->|read-only OAuth| GS
    GW -->|signed POST| WE
    WE -->|targeted fetch| GS
    GS -->|store_page| GR
    GR -->|parsers| N
    R -->|read-only API key| RP
    RP --> RR
    RR -->|garmin_normalize| N
    N --> D
    N --> P[source precedence,<br/>readers]
    D --> P
    P --> API[app/api]
    API --> UI[Dashboard and CSV]
    API --> U[Umay tools]
    U -.->|optional, via the bridge| O[OpenAI]
```

- **Google.** The scheduler runs the daily and weekly reconciliations; a signed webhook stores
  its exact body in `webhook_events` and triggers a fetch of the notified window; a backfill
  walks history by type and time window. Every response body is kept byte for byte
  (`raw_api_responses`), even when the request fails part-way. Each page then goes to the
  storage service, which writes each record's current payload (`raw_health_data`, earlier
  payloads in `raw_health_data_versions`), its identity and sightings, the normalized rows and
  the page checkpoint in one transaction.
- **Garmin** (optional, `HEALTHOS_SOURCES=garmin`). The `garmin-pull` job and `healthos garmin-backfill`
  fetch per kind and day; each exchange is kept in `garmin_api_responses`, each distinct record
  payload in `garmin_raw_records`, and the newest records are normalized into the same
  normalized tables, tagged `provider = 'GARMIN'`.
- **Apple Health** is imported only in the published GitHub snapshot (not in this tree; see
  [Sources](reference/sources.md#what-is-not-in-this-tree)), through an extension that adds its
  own raw tables and normalizes with `provider = 'APPLE'` the same way.
- **Reading.** Readers count a Google row only through its live raw row (deleted at the source,
  quarantined and superseded rows are skipped); when two sources report the same thing, the
  precedence policy selects one and returns the others as alternatives, never a sum. The
  dashboard, the CSV export and Umay's `health_archive` tool all go through these readers.
- **Umay** (optional). The chat runs through a separate Codex bridge on the host. The model asks
  the backend's read-only `health_archive` tool for bounded slices; the backend answers from the
  readers above. What is sent is described in [Privacy](PRIVACY.md#umay-and-openai).

## Why each source keeps its own raw tables

Google, Garmin and Apple each have their own raw tables. They are not merged into one table,
on purpose:

- **Different shapes of truth.** Google returns *records* with resource names (or none, see
  below), paginated, with webhooks and deletion notifications. Garmin returns per-kind
  *daily summaries and activities* that change during the day and have no deletion signal; a
  changed day is a new row. Apple arrives as a file export or a push, not as a pull. A shared
  raw table would need the lowest common denominator of these and lose what each one promises.
- **Different guarantees, enforced per table.** The triggers that make the archive
  append-only are specific: a Google payload change becomes a version row before the current
  payload changes; a Garmin record never changes at all, a new payload is a new row. One table
  with per-row rules would be harder to verify and easier to get wrong.
- **No destructive migrations.** Raw data is never rewritten. Adding a source therefore means
  adding tables next to the existing ones; moving existing rows into a new combined table would
  be exactly the kind of rewrite the archive forbids.
- **Isolation of the Google machinery.** Identity, sightings, deletion marking and coverage
  read `raw_health_data` only. A Garmin row has no `raw_health_data` row, so a complete Google
  fetch can never mark a Garmin record deleted, and Garmin can never be chained to a Google
  device.

What *is* shared is the **normalized** layer: one `steps`, one `exercise_sessions`, one
`heart_rate` and so on, with a `provider` column. That keeps one set of readers, one
precedence resolver and one workout timeline for every source. The measured reasons for adding
only columns there (no table rebuild, every trigger and view kept) are in the
[technical reference](reference/garmin.md#garmin-connect-personal-api).

## Why the identity and source-chain layer is this large

The identity layer (`app/parsers/identity.py`, `app/models/identity*.py`,
`app/services/identities/identity*.py`, `app/services/identities/source_chain*.py`) is about thirty modules and a
quarter of the backend's code. That is deliberate; the maintainer chose this design and it is
not a candidate for simplification. It answers one question that the archive cannot avoid:
**is this record one I already have?**

- **Most Google records have no id.** Heart-rate samples, steps, zones, intervals and daily
  summaries arrive without a resource name, so their identity has to be derived from the
  record. Deriving it from the whole record does not work: Google renames devices
  (`displayName`), Apple Health drops a package suffix, Google adds civil times and offsets to
  intervals, and a sedentary period's end keeps growing. Each of these used to turn the same
  record into a new row.
- **A rename and a second device look the same in one record.** Fitbit sends no serial number.
  Two fingerprints with the same key are either one device before and after a rename (one
  value) or two devices that measured the same instant (two values). The write path cannot
  tell them apart from one page, so it never guesses: an unknown fingerprint gets rows of its
  own. A nightly reconciler decides per source from the sightings' timelines and from evidence
  collected across fetches, and leaves every ambiguous case to the owner (`healthos
  identity-decide`).
- **Nothing is deleted, so decisions are history too.** Because no row may be deleted or
  re-keyed, a duplicate is never removed: it is *superseded* by a link, readers skip it while
  the link is in force, and a link can be refuted or reversed later without losing anything.
  Chains, decisions, evidence, sightings and supersessions are therefore append-only tables of
  their own, each with a trigger, and `healthos verify` checks them.
- **Several sources, versions and deletions interact.** A deletion seen in one source form
  must not mark a record that the renamed form still returns; a version of a record must stay
  attached to the right row; a quarantined or deleted holder must not hide a live record. Each
  of these rules is small, but each needs its own code and tests.

The alternative designs (hashing the whole record, merging rows, or deleting duplicates) are
smaller, and each of them either counts the same measurement twice or destroys evidence. The
detailed rules are in the [technical reference](reference/identity-and-versions.md#record-identity).

## Runtime

- **Scheduled jobs** (APScheduler, in the app process): the daily and the weekly Google
  reconciliation, the identity reconciler after them, the pipeline watchdog, the freshness and
  derived-day refreshes and, when enabled, the Garmin pull. Syncs of one data type and
  backfill jobs cannot run twice at once, even across processes; the identity reconciler and
  deletion marking take a per-type PostgreSQL advisory lock.
- **Recovery.** At start-up, work left running by a stopped process is marked failed or
  reclaimed; the watchdog detects stale data and runs a bounded recovery.
- **Backups** run outside the app, from a host timer (`scripts/backup.sh`), and are kept
  forever unless the owner opts into retention; the app only reports their status.

## Dashboard and Umay details

### HealthOS dashboard

The application includes a Turkish dashboard at `http://healthos.localhost:8000/`.
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
in `app/services/umay/codex_chat.py`. Examples are style guidance, not user health data.
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

4. Open the round Umay button on the dashboard at `http://healthos.localhost:8000/`.
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
with desktop plugins, shell, browser, computer-use, image-reading and multi-agent tools disabled.
The bridge requires a random bearer token; only the backend receives that token.
Token and credentials are never included in frontend bundles or tool responses.
Manual logs are in `healthos-bridge/bridge.log`; service logs go to the journal.
The isolated home is mode 0700, its token, login and configuration files mode 0600.
Windows-specific steps (an isolated npm-installed CLI run through Node) are in
[legacy/WINDOWS.md](legacy/WINDOWS.md#umay-bridge-on-windows).

**Runtime verification (fail closed).** Flags alone do not bound the CLI: with CLI 0.161.0 the
bridge model's own metadata re-enabled sub-agents although `multi_agent=false`, and the built-in
`view_image` tool read images outside the workspace. Before every start the launcher therefore
measures the native binary it will run (the first `codex` on `PATH` that is one or, through npm,
starts one; shell shims are skipped; or the absolute path in `CODEX_BRIDGE_CLI`) in a
throw-away home against a synthetic loopback model ([`codex_runtime_probe.py`](../app/services/umay/codex_runtime_probe.py)):
the tool catalog may contain only the health tool and the CLI's code-mode wrapper; forced
`view_image` (absolute, relative, symlink), `spawn_agent`/`followup_task` and `apply_patch` calls
must return no image, no child agent and no write; sandboxed writes and loopback connections must
fail; the health tool must still reach the application callback. The bridge configuration sets
`features.view_image=false`, a dispatch limit of one agent thread
(`features.multi_agent_v2.max_concurrent_threads_per_session = 1`) and a bridge-owned
`model_catalog_json` derived from the binary's bundled catalog without the multi-agent and patch
tools ([`codex_bridge_policy.py`](../app/services/umay/codex_bridge_policy.py)). Only measured CLI
releases are accepted (`SUPPORTED_CLI_VERSIONS`; 0.161.0 as of 11 November 2026). An unknown version or any
failed check stops the launcher before the login is copied; the result, without secrets, is in
`healthos-bridge/runtime-attestation.json`. Before it opens a thread, the chat reads the bridge's
effective configuration (app-server `config/read`) and refuses a bridge whose limits differ or whose
model catalog is not named after the current policy fingerprint, such as a bridge left running from
an older launcher or started by hand; every thread repeats all limits as well. The chat also
refuses a thread whose CLI version, model, approval policy or sandbox differ. The launcher reports a
running bridge it cannot reuse, or any process on the bridge address, instead of starting next to
it: stop that process and run the launcher again. The bridge app-server gets the probe's
environment allowlist (its own `HOME` and `CODEX_HOME`, `TMPDIR`, `LANG`, `PATH`) plus proxy and CA
variables, never the rest of the operator's environment. The rest of HealthOS keeps working. To admit a
new CLI release, run `HEALTHOS_CODEX_CLI=/path/to/codex pytest tests/test_codex_bridge_runtime.py`
and add the version only when it passes. Verification recorded on 11 November 2026: measured on macOS arm64 with a synthetic provider; a real
ChatGPT session, Linux and Windows are checked by the same probe at each start but were not part of
that measurement.

**TLS front.** On its own the app-server speaks `ws://` with a bearer token, so anything holding
its address while it is down would be handed the token and the conversation. The launcher therefore
moves the app-server to a random private loopback port behind `scripts/codex_bridge_front.py`. The
front serves TLS 1.3 on the public bridge address with an Ed25519 key that both ends derive from the
bridge token (HKDF, `app/services/umay/codex_bridge_tls.py`); the app trusts only that certificate and
sends no bearer or message before the handshake proves it. The front checks the bearer, confirms the
listener on the inner port is the expected app-server (`config/read` must answer with the launcher's
random directory) and replaces the bearer with a throw-away inner token, so the app-server never sees
the bridge token. `CODEX_BRIDGE_URL` is dialled as `wss://` even when `.env` says `ws://`. An old
plain bridge is refused with a clear message: restart the launcher once, with `.env` unchanged. Verification recorded on 11 November 2026: run
with the real CLI 0.161.0 and a fake login on loopback; a real ChatGPT turn was not exercised. Windows:
killing the front with `TerminateProcess` can leave its Codex child running.

**What the front lets through.** The app-server is a general agent host: measured with CLI 0.161.0,
its bearer also buys `command/exec` with a sandbox the caller picks (a shell as the operating-system
user, whatever the thread settings say), configuration writes and threads with any tools. So the
WebSocket ends at the front: every client frame is parsed, compared with the exact shape the chat
sends and forwarded re-serialized (`app/services/umay/codex_bridge_filter.py`).

| RPC | Why the chat needs it | Constraint |
| --- | --- | --- |
| `initialize` | Opens every connection | Exact params, once, first |
| `initialized` | Handshake notification | No id, empty params, once |
| `account/read` | The chat refuses without a ChatGPT account | Empty params, once per connection, before thread creation; reply contains only required account/policy fields |
| `config/read` | Policy check before any thread | Empty params, once per connection, before thread creation; reply contains only required account/policy fields |
| `thread/start` | The chat's one thread | Canonical-JSON equal to `chat_thread_params()`, once |
| `turn/start` | Each user question | Exact keys, own thread, policy model and effort, exactly two text items |
| `turn/interrupt` | Cancel, browser leaving | Exact keys, own thread |

Replies to the app-server's own requests are filtered too: only unanswered ids it issued,
with a `result` only for `item/tool/call` in the chat's shape. Anything else (another method, a binary
or batch frame, malformed JSON, duplicate keys, `NaN`) gets a JSON-RPC error when the frame has a usable
id and the connection closes with 1008; a frame over 8 MiB closes with 1009. That limit is a judgement,
not a measured worst case: a larger tool result would end the chat. At most 16 connections are
admitted. A holder of the bridge token can still run turns with its own text on the fixed thread
(using the subscription) and read the limited account/configuration fields; no shell, configuration write or other thread.

**Restarting after a change.** The front reads the thread definition and persona once, at start, and
the policy fingerprint covers `codex_bridge_policy.py`, `codex_runtime_probe.py`, `codex_runtime_rpc.py`,
`codex_bridge_filter.py`, `codex_chat_thread.py`, `archive_query.py` and `app/personas/umay/soul.md`. After a
change to any of them the running bridge no longer matches: the chat refuses it and the log says to restart
it, so restart the bridge ([update steps](RUNBOOK.md#update-the-application)). A front started before this
filter existed forwards everything until it is restarted, though the chat refuses to use it.

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
native backend outside Docker, set `CODEX_BRIDGE_URL=wss://127.0.0.1:8766` instead.
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

Open `http://healthos.localhost:8000/`. This Compose file runs in production mode, which serves no API
documentation; see [Local API](reference/api.md#local-api).
After frontend changes, run `docker compose up -d --build app` to update this
permanent dashboard. `npm run build` alone only updates the host files; an
existing container keeps its bundled frontend. Port `5173` is a temporary Vite
development preview, not the dashboard restored by Docker after a restart.
The Sistem page has four groups: **Durum** (coverage, Google notifications, connection), **Cihazlar**,
**Eşitleme** (data types) and **Ayarlar** (appearance, about). Each group has its own scroller, so the
page itself never scrolls; below 56.25em of width or 38em of height (900 × 608 px at the default
font size) the groups become tabs (`frontend/src/system-page.js`). Verification recorded on
11 November 2026: checked in Chromium; a real screen reader or phone was not tested.

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
(the active flags, e.g. `["test"]`) to each entry and still lists flagged
ones; the profile card, the 3D body and Umay's daily data
(`dashboard_data`) leave them out and use the entry before, or show no value.
Readers decide it in one place: `app/services/archive/manual_flags.py`,
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
                                 FastAPI HealthOS
                         ↙ raw JSON      normalized tables ↘
                                  PostgreSQL
                         ↙                           ↘
                    pg_dump                    SQL/Grafana/Pandas
```

The core has two containers: `app` and `postgres`. APScheduler runs daily and weekly reconciliation inside the app, avoiding a mandatory queue or hosted scheduler. Run only one scheduler-enabled app replica; if scaling the API, set `RECONCILIATION_ENABLED=false` on all but one worker.

## Further reading

| Topic | Where |
| --- | --- |
| Settings, tables, API routes, identity rules | [Technical reference](REFERENCE.md) |
| Updates, migrations, recovery | [Runbook](RUNBOOK.md) |
| Server installation | [Deployment](DEPLOYMENT.md) |
| Validation commands, repository map | [Development](DEVELOPMENT.md) |
| What leaves the machine | [Privacy](PRIVACY.md) |
