# HealthOS reference — Overview

Part of the [technical reference](../REFERENCE.md).

A fully self-hosted, subscription-free, read-only archive of health and fitness data from the sources you select ([Sources](sources.md#sources)): the **Garmin Connect Personal API** and the **Google Health API v4** in this tree. It preserves every source payload before normalization and maintains analytics-friendly PostgreSQL tables for heart rate, HRV, oxygen saturation, sleep, steps, exercise, and long-tail scalar metrics.

> This project uses `https://health.googleapis.com/v4`, the current Google Health API. It does not use or add functionality against the deprecated Fitbit Web API. Support for the legacy Fitbit Web API ended on September 30, 2026, and the API shut down completely on October 30, 2026; because for Fitbit data HealthOS uses only Google Health API v4, Google OAuth, and `googlehealth.*` scopes, that shutdown does not affect it.

## Design principles

`RAW FIRST → NORMALIZE SECOND → ANALYZE LATER`

- Each source remains the source of truth for its own data. This application only makes read requests to user data endpoints.
- Every HTTP response body Google sends is kept byte for byte in `raw_api_responses`, and every
  record parsed from it in `raw_health_data`; unknown fields are never dropped from the archive
  (see [What is preserved where](data-safety.md#what-is-preserved-where)).
- A Google resource name is the primary source identifier. Where a response lacks one, a
  deterministic identity is derived from type, physical time, discriminators and the stable
  data-source fields, so a renamed device or a reshaped interval is still the same record (see
  [Record identity](identity-and-versions.md#record-identity)).
- Payload hashes distinguish unchanged records from source corrections. A correction never
  overwrites the old payload: every version is kept in `raw_health_data_versions` (see
  [Raw record versions](identity-and-versions.md#raw-record-versions)).
- Raw and normalized UPSERTs plus their page checkpoint commit in one transaction.
- Backfill progress is stored per data type and time window, including the next page token.
- A page whose `nextPageToken` repeats one of the same attempt is stored like any page (records,
  responses, deduplication), but its token is never saved as a checkpoint and the window's
  watermark does not advance; the fetch then fails with `repeated_page_token` and resumes from
  the previous token.
- Incremental sync deliberately overlaps previous checkpoints to catch late or enriched data.
- Webhooks trigger retrieval; they are not treated as the dataset. Reconciliation repairs missed notifications.
- A webhook `DELETE` notification never deletes archive rows. The follow-up read and webhook audit record are retained.

## Free and private by default

There is no telemetry, tracking SDK, external logging service, hosted database, paid synchronization service, API proxy, or mandatory cloud backup. The mandatory software components are open-source and run on your hardware. Google Cloud/API account policies and any infrastructure or domain you independently choose are outside the project; no paid product is required by this code.

PostgreSQL has no published host port in the production Compose file. FastAPI is published only on `127.0.0.1`. The application container runs as an unprivileged UID with all Linux capabilities dropped, `no-new-privileges`, and a read-only root filesystem. PostgreSQL also drops all capabilities except the five its entrypoint needs to hand the data directory to the `postgres` user (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`) and runs with `no-new-privileges`; the on-demand backup container keeps no capabilities and a read-only root. Container logs use the `json-file` driver with rotation (`max-size: 20m`, `max-file: 5`) for PostgreSQL, the app, and Caddy. Put a self-hosted reverse proxy with HTTPS in front of it before exposing the webhook. Secrets are environment variables; tokens are never logged. A failure is logged without its message, traceback or SQL parameters, which can quote a health record or a device name: only the exception class chain, the PostgreSQL SQLSTATE and the code locations (`module:function:line`, `error_frames`); the application's database engine also hides bound parameters in its errors. Logs written before this change are not rewritten. OAuth refresh tokens obtained by the callback are Fernet-encrypted in PostgreSQL using a key that stays in the environment.

## Non-goals

- No medical diagnosis or health advice; AI analysis exists only in the optional, on-request Umay chat.
- No writes, patches, or deletes against Google Health.
- No legacy Fitbit Web API compatibility layer.
- No mandatory hosted service, paid API, or public database exposure.
