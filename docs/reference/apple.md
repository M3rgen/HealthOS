# HealthOS reference — Apple Health

Part of the [technical reference](../REFERENCE.md).

## Apple Health (GitHub version)

Apple Watch and iPhone data can be brought into HealthOS directly, through two gates. Both are
part of the published (GitHub) version only; they are kept apart from the main line, merged only
into the published snapshot, and are not installed on the maintainer's own server, which receives
Apple data, if at all, through Google Health (`HEALTH_KIT` records). Neither gate is a new external
service: HealthOS fetches nothing from Apple. Gate 2 is a file the owner exports from the Health
app; gate 1 is a request the owner's own phone sends (Health Auto Export is an app on the phone,
not a cloud API), so the rule against new external services is not touched.

| Gate | What arrives | Default | Endpoint |
|---|---|---|---|
| 1. Health Auto Export receiver | JSON from the [Health Auto Export](https://help.healthyapps.dev/en/health-auto-export/export-format) iOS app's *REST API* automation (paid feature of that app) | **off** | `POST /webhooks/apple-health` |
| 2. Manual export upload | the Health app's `export.zip` (or the `export.xml` inside it) | available | System › "Apple Sağlık içe aktar", `POST /api/apple/import`, or `healthos apple-import PATH` |

**Gate 2: export.zip.** In the Health app: profile picture › *Export All Health Data*. The
archive is a zip with `apple_health_export/export.xml` (its name is localized in some languages;
the importer finds the file by its content: the first `.xml` entry other than `export_cda.xml`
whose first 64 KiB contain `<HealthData`), `export_cda.xml`, workout routes (`.gpx`) and ECG
files. Only the main XML is read; the other entries are skipped (their file names are kept in a
workout's `routes`). The upload is streamed: the zip is read in arrival order (local file headers,
deflate or stored, zip64), the XML is parsed with an expat push parser (no tree is built, memory
stays constant: a 50 MB synthetic stream peaks at a few MB in the tests), and every 2,000 records
are archived, normalized and committed. Nothing is written to disk or held whole in memory, so a
multi-gigabyte `export.xml` works; the request simply runs until the file is read. Unzipping and
parsing run in a worker thread, one batch of records at a time, so the rest of the API (`/health`,
the dashboard) keeps answering while an import runs. The XML's
internal DTD (ELEMENT/ATTLIST declarations) is accepted; any `<!ENTITY` declaration, an external
DTD and undeclared entities are refused (no entity expansion, no external fetch). Limits:
`APPLE_IMPORT_MAX_UPLOAD_BYTES` (default 8 GiB) for the upload; against zip bombs,
`APPLE_IMPORT_MAX_UNCOMPRESSED` (default 16 GiB; real exports are a few GB) decompressed per zip
and at most 100 decompressed bytes per zip byte read once the first 64 MiB are out (real exports
compress about 10–20:1, a deflate bomb about 1000:1); past either the upload answers 413 (the CLI
import holds the same limits). Encrypted entries and stored entries with a data descriptor are
refused. One
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
`.env`, the database or a log). `healthos apple-token` writes a new token (directory 700, file
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
sent again is not stored twice and answers `duplicate`, also when the app delivers the same body
twice at the same moment (the second request waits for the first and stores, archives and
overwrites nothing). From `data.metrics[]` every data point
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
[Cloudflare Tunnel](../CLOUDFLARE_TUNNEL.md) setup of the Google webhook works the same way with an
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
are append-only (UPDATE/DELETE/TRUNCATE refused by trigger, checked by `healthos verify`);
`apple_raw_records` holds one row per record version (unique channel, kind, key and body
SHA-256; `channel` `export` or `hae`, `kind` `quantity`/`category`/`record`/`workout`/
`activity-summary`/`hae-metric`/`hae-workout`, `type` the HealthKit identifier or the metric
name, the body as received, source name, device model, start and end). `apple_imports` (status,
progress and counts of each upload, CLI import or receiver request) and `apple_sources` (per
Health source: device model, first and last import, newest data, data types, channels) are
bookkeeping. The downgrade refuses while any of the four tables holds a row: the archive tables
by their own guard, the import history and the source summary by the general downgrade guard
(`app/db/downgrade_guard.py`), since neither can be rebuilt once dropped.

**Normalization.** Records go into the shared normalized tables with `provider = 'APPLE'`
(`app/services/providers/apple_normalize.py`), idempotent by comparison like Garmin's: a second run writes
nothing; when several versions of one record arrive, the newest wins.
`healthos apple-normalize [--since YYYY-MM-DD]` rebuilds the rows from the archive.

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
(`app/services/providers/apple_readers.py`) therefore never add sources or gates together:

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

**Precedence and Cihazlarım.** Apple values take part in [Source precedence](source-precedence.md#source-precedence)
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
healthos apple-token [--rotate] [--file PATH]        # receiver token: written once, printed once
healthos apple-import PATH                           # export.zip or export.xml (native installs; no size limit)
healthos apple-status                                # receiver settings and the last 10 imports (JSON)
healthos apple-normalize [--since YYYY-MM-DD]        # rebuild the normalized Apple rows from the archive
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
existing revision (the current one is `20261111_a002`, joining `20261109_0029`; the main line's
empty bridge `20261109_p001` never stands in for the real `20261107_a001` here).
`tests/test_apple_migration.py` and the snapshot script fail while there are
two heads. The lines the branch changes in shared files (`tests/test_migrations.py` table count,
`scripts/verify_compose.py` expected revision, the table lists of a few migration tests) are the
expected merge conflicts.
