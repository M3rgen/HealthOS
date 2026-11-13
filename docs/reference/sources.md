# HealthOS reference — Sources

Part of the [technical reference](../REFERENCE.md).

## Sources

`HEALTHOS_SOURCES` selects which sources run. It is read once at startup; `active_sources` in
`/health` shows the result. The template in `.env.example` sets `none`.

| Value | What it does in this tree | What it needs | Availability |
|---|---|---|---|
| `none` | No source. The archive, the dashboard and manual entries work; the Google and Garmin routes and jobs are not registered, and the CLI's Google commands stop with `Bu kurulumda Google kaynağı kapalı.` (exit 1), its Garmin commands with `Garmin entegrasyonu bu çalışma zamanında yok.` (exit 2) | `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, `APP_UI_TOKEN` | Everyone |
| `garmin` | Pulls the owner's Garmin Connect account every two hours into its own raw tables, normalizes into the shared tables; the dashboard shows it | A Personal API key in a file, readable by the app ([quickstart](../../README.md#3-connect-garmin), [reference](garmin.md#garmin-connect-personal-api)) | You create the key yourself on [Garmin's Personal API page](https://developer.garmin.com/gc-developer-program/personal/personal-api) |
| `google` | OAuth, sync, reconciliation and webhooks against Google Health API v4 | An eligible Google Cloud project, an OAuth client, the Google values in `.env` | Only with an already approved project; Google stated on October 3, 2026 that it is not onboarding new Google Health API projects |
| `apple` | **Accepted, but adds nothing**, unless your checkout contains `app/apple/` | See below | See below |

Sources combine, for example `HEALTHOS_SOURCES=garmin,google`. In production the app refuses to
start when a selected source is missing what it needs, and logs
`unsafe or missing production configuration: <names>`.

### What is not in this tree

This repository's development tree has no Apple Health importer: no `app/apple/` package, no upload
page, no `/api/apple/*` or `/webhooks/apple-health` route, no Apple tables. `HEALTHOS_SOURCES=apple`
passes validation and stays in `active_sources`, but it imports nothing. The app says so: a warning
at startup (`SOURCE_IMPORTER_NOT_INSTALLED`), the reason `apple_importer_not_installed` in `/health`
(the overall status is unchanged) and "Kurulu değil" in the Sistem panel. It is reserved for the
extension.

The importer is included in the published release; it is absent from this development tree
and is not covered by this tree's tests. To tell which one you have, look for `app/apple/` in your
checkout. Likewise, the guided installers (`install.sh`, `install.ps1`) and their guides are in the
published release, not in this tree.

## Sample data (Örnek veri)

Without a Garmin or Google account you can still see what HealthOS shows: load about 90 days of
synthetic data for an invented person (steps, heart rate every ten minutes, sleep, resting heart
rate, HRV, SpO₂, weight and workouts). Nothing is read from an account or the network.

- **From the interface:** on an empty installation with no working source, the empty overview and
  Sistem › Bağlantı offer **Örnek veriyle dene** (*Try with sample data*). While the dashboard shows
  it, a banner on every page says so and has **Örnek veriyi kaldır** (*Remove sample data*).
- **From the command line:** `healthos demo` loads it (`--timezone Europe/Istanbul` fills that time
  zone's calendar days; the default is UTC) and `healthos demo --remove` deletes it again. The
  interface uses `POST` and `DELETE /api/demo` (with `X-HealthOS-Request: 1`); `GET /api/demo` says
  whether it is loaded.

It is labelled everywhere it appears: its own source code `DEMO` ("Örnek veri" / "Sample data"),
record keys starting `demo:`, and the device name "HealthOS sample data (synthetic)". It is written
to the shared normalized tables only, never to the raw archive, because no source returned it.
Loading is refused (exit 1, HTTP 409) once the archive holds a Google or Garmin record, so it never
mixes with real data; your manual entries are left alone. Removing deletes exactly the rows marked
as sample data in both ways and refuses, deleting nothing, if any row carries only one of the two
marks. The same seed and day always give the same values, so loading again only adds the days that
are missing.
