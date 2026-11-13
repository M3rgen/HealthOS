# HealthOS — Technical Reference

The technical reference is split by topic; this page is its table of contents.

## Topics

| Topic | Page | Contents |
|---|---|---|
| Overview | [reference/overview.md](reference/overview.md) | What the archive is, its design principles, privacy and non-goals |
| Architecture | [ARCHITECTURE.md](ARCHITECTURE.md) | Dashboard, source-separated daily foundation, interpretation budgets, Umay personality and bridge |
| Setup | [reference/setup.md](reference/setup.md) | Requirements, Docker and native setup, OAuth and Google Health |
| Configuration | [reference/config.md](reference/config.md) | Environment variables |
| Sources | [reference/sources.md](reference/sources.md) | What each `HEALTHOS_SOURCES` value does, and what is not in this tree |
| Sync | [reference/sync.md](reference/sync.md) | Google Health API constraints, historical backfill, incremental sync and source deletions |
| Record identity and versions | [reference/identity-and-versions.md](reference/identity-and-versions.md) | Record identity, hollow names, devices, raw record versions and recovering them |
| Data safety | [reference/data-safety.md](reference/data-safety.md) | What is preserved where (verbatim responses, derived tables, write-path audit), backup and restore, integrity verification |
| Webhooks | [reference/webhooks.md](reference/webhooks.md) | Subscriber configuration, Cloudflare Tunnel and direct Caddy receivers |
| Reconciliation | [reference/reconciliation.md](reference/reconciliation.md) | Scheduled reconciliation and outage recovery |
| Garmin | [reference/garmin.md](reference/garmin.md) | Garmin Connect Personal API source |
| Apple Health | [reference/apple.md](reference/apple.md) | Health Auto Export receiver and `export.zip` import (published release only) |
| Source precedence | [reference/source-precedence.md](reference/source-precedence.md) | Which source is shown first when several report the same data |
| Local API | [reference/api.md](reference/api.md) | HTTP routes of the local API |
| Troubleshooting | [reference/troubleshooting.md](reference/troubleshooting.md) | Common failures |
| Analytics compatibility | [reference/analytics.md](reference/analytics.md) | Analytics views and quarantined rollup records |
| Command-line interface | [reference/cli.md](reference/cli.md) | The `healthos` commands this reference documents, each with where it is described |

## Moved sections

Every heading of the former single-page reference is kept below so that old `#anchor` deep links
into this page still land here; follow the heading's link to the section's new location.

### [Design principles](reference/overview.md#design-principles)

### [Architecture](ARCHITECTURE.md)

### [HealthOS dashboard](ARCHITECTURE.md#healthos-dashboard)

### [Source-separated daily foundation](ARCHITECTURE.md#source-separated-daily-foundation)

### [Interpretation budgets](ARCHITECTURE.md#interpretation-budgets)

### [Umay personality](ARCHITECTURE.md#umay-personality)

### [Local Codex chat (Umay bridge)](ARCHITECTURE.md#local-codex-chat-umay-bridge)

### [Current Google Health API constraints](reference/sync.md#current-google-health-api-constraints)

### [Free and private by default](reference/overview.md#free-and-private-by-default)

### [Requirements](reference/setup.md#requirements)

### [Docker setup](reference/setup.md#docker-setup)

### [Native development](reference/setup.md#native-development)

### [Configuration](reference/config.md#configuration)

### [OAuth and Google Health setup](reference/setup.md#oauth-and-google-health-setup)

### [Historical backfill](reference/sync.md#historical-backfill)

### [Incremental sync](reference/sync.md#incremental-sync)

### [Source deletions](reference/sync.md#source-deletions)

### [Record identity](reference/identity-and-versions.md#record-identity)

### [Hollow Google names](reference/identity-and-versions.md#hollow-google-names)

### [Devices](reference/identity-and-versions.md#devices)

### [Raw record versions](reference/identity-and-versions.md#raw-record-versions)

### [Recovering versions from old dumps](reference/identity-and-versions.md#recovering-versions-from-old-dumps)

### [What is preserved where](reference/data-safety.md#what-is-preserved-where)

### [Verbatim Google responses](reference/data-safety.md#verbatim-google-responses)

### [Records jsonb cannot hold](reference/data-safety.md#records-jsonb-cannot-hold)

### [Source deletion events](reference/data-safety.md#source-deletion-events)

### [Write paths (audit)](reference/data-safety.md#write-paths-audit)

### [Derived tables](reference/data-safety.md#derived-tables)

### [Webhooks](reference/webhooks.md#webhooks)

### [Cloudflare Tunnel receiver (deployed)](reference/webhooks.md#cloudflare-tunnel-receiver-deployed)

### [Direct Caddy receiver (unused alternative)](reference/webhooks.md#direct-caddy-receiver-unused-alternative)

### [Reconciliation](reference/reconciliation.md#reconciliation)

### [Outage detection and bounded automatic recovery](reference/reconciliation.md#outage-detection-and-bounded-automatic-recovery)

### [Garmin Connect Personal API](reference/garmin.md#garmin-connect-personal-api)

### [Apple Health (GitHub version)](reference/apple.md#apple-health-github-version)

### [Source precedence](reference/source-precedence.md#source-precedence)

### [Local API](reference/api.md#local-api)

### [Database backup and restore](reference/data-safety.md#database-backup-and-restore)

### [Integrity verification](reference/data-safety.md#integrity-verification)

### [Troubleshooting](reference/troubleshooting.md#troubleshooting)

### [Analytics compatibility](reference/analytics.md#analytics-compatibility)

### [Quarantined rollup records](reference/analytics.md#quarantined-rollup-records)

### [Non-goals](reference/overview.md#non-goals)
