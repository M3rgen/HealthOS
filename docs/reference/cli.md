# HealthOS reference — Command-line interface

Part of the [technical reference](../REFERENCE.md).

Run `healthos` inside the container (`docker compose exec app healthos …`) or in the native
environment. The deprecated former name of the command is described in
[DEVELOPMENT.md](../DEVELOPMENT.md#repository-map).

This page lists only the commands this reference documents, in the reference's own words, with a
link to where each one is described. It is not a complete option reference.

An unexpected failure prints only `Komut tamamlanamadı. Hata türü: <class>` on stderr and exits 1; the
log entry goes through the same secret redaction as the server's logs. Google commands stop with
`Bu kurulumda Google kaynağı kapalı.` (exit 1) and the Garmin commands with
`Garmin entegrasyonu bu çalışma zamanında yok.` (exit 2) when their source is not in `HEALTHOS_SOURCES`.

| Command | What the reference says | Described in |
|---|---|---|
| `healthos probe --days 30` | Metadata-only capability probe: requests at most one recent point per configured type and reports accessibility, timestamps and top-level field names without returning or storing health values | [OAuth and Google Health setup](setup.md#oauth-and-google-health-setup) |
| `healthos sync`, `healthos sync <data type>` (for example `sync steps`, `sync sleep`) | Writes retrieved records to the configured database | [OAuth and Google Health setup](setup.md#oauth-and-google-health-setup), [Incremental sync](sync.md#incremental-sync) |
| `healthos status` | Local status: lists, among others, what the identity reconciler leaves to the owner (`identity_reconcile`, `identity_review_suggested`, deferred deletions) and `failed_nights` | [Record identity](identity-and-versions.md#record-identity), [Reconciliation](reconciliation.md#reconciliation) |
| `healthos backfill` (`--data-type <type>`, `--reset-abandoned`) | Starts a persisted backfill and waits; `--reset-abandoned` requeues abandoned windows, then runs | [Historical backfill](sync.md#historical-backfill) |
| `healthos reconcile --days N` | Runs reconciliation manually, for example `--days 7` or `--days 90` | [Reconciliation](reconciliation.md#reconciliation) |
| `healthos verify` | Integrity verification: checks the protective triggers, recalculates payload hashes and checks versions, links and chains; exits 1 on any finding | [Integrity verification](data-safety.md#integrity-verification) |
| `healthos identity-backfill` (`--incremental`, `--dry-run`, `--batch-size`, `--after`) | The reconciler: the only place that decides chains, and it links what it decided | [Record identity](identity-and-versions.md#record-identity) |
| `healthos identity-decide` (`link`, `reject`, `confirm`, `distinct`, `not-distinct`; `--dry-run`, `--data-type`) | Deciding by hand; each decision is appended to `raw_source_decisions` and applied at once | [Record identity](identity-and-versions.md#record-identity) |
| `healthos identity-sources --data-type <type>` | Lists the sources with their fingerprints, spans, devices, chains, decisions and open cases | [Record identity](identity-and-versions.md#record-identity) |
| `healthos history` (`--data-type <type>`, `--since`, `--limit`, `--id`, `--no-payload`) | Lists versions from the command line (JSON, oldest version first; no secrets; payloads are included unless `--no-payload`), with source-deletion events next to the versions | [Raw record versions](identity-and-versions.md#raw-record-versions), [Source deletion events](data-safety.md#source-deletion-events) |
| `healthos import-versions --from-db <database>` | Reads a restored dump and merges each record's dump rows into its timeline; `--dry-run` reports what it would skip | [Recovering versions from old dumps](identity-and-versions.md#recovering-versions-from-old-dumps) |
| `healthos responses` (`--data-type`, `--since`, `--limit`, `--context`) | Lists stored Google responses: metadata, newest first | [Verbatim Google responses](data-safety.md#verbatim-google-responses) |
| `healthos export-response <id>` (`--output <file>`) | Writes the exact stored bytes of a response, metadata on stderr; refuses (exit 1) when the bytes no longer match their SHA-256 | [Verbatim Google responses](data-safety.md#verbatim-google-responses) |
| `healthos rejections` (`--element <id>`, `--ack <id>`) | Lists rejected records (newest first, metadata only, with the ack in force), prints the parsed record of a rejection, or acknowledges rejections up to an id | [Records jsonb cannot hold](data-safety.md#records-jsonb-cannot-hold) |
| `healthos webhook-readiness` | Run before subscriber registration; reports only booleans and never prints the endpoint authorization value | [Webhooks](webhooks.md#webhooks) |
| `healthos webhook-status` | Part of the local status checks of the tunnel receiver | [Cloudflare Tunnel receiver](webhooks.md#cloudflare-tunnel-receiver-deployed) |
| `healthos webhook-replay-pending` | Retries stored `pending` and `failed` webhook elements; completed elements are not re-fetched | [Cloudflare Tunnel receiver](webhooks.md#cloudflare-tunnel-receiver-deployed) |
| `healthos google-rebind --confirm-account-change` | Deliberately changes the Google account: prompts for the new account's refresh token (hidden), verifies it and appends a credential version and, for a different account, a `google_account_rebind_events` row; the archive is kept. Without the flag it exits 2 | [OAuth and Google Health setup](setup.md#oauth-and-google-health-setup) |
| `healthos garmin-status` | Enabled, key file present, last success/error, per kind | [Garmin Connect Personal API](garmin.md#garmin-connect-personal-api) |
| `healthos garmin-pull` (`--kind <kind>`) | One incremental run | [Garmin Connect Personal API](garmin.md#garmin-connect-personal-api) |
| `healthos garmin-backfill --since <date>` (`--until`, `--dry-run`, `--kind`) | Chunks the range per kind by its limits and keeps a checkpoint per kind | [Garmin Connect Personal API](garmin.md#garmin-connect-personal-api) |
