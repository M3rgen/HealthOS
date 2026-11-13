# HealthOS reference — Sync

Part of the [technical reference](../REFERENCE.md).

## Current Google Health API constraints

The implementation follows the official [v4 REST reference](https://developers.google.com/health/reference/rest), [data-type table](https://developers.google.com/health/data-types), [endpoint guide](https://developers.google.com/health/endpoints), and [webhook guide](https://developers.google.com/health/webhooks).

- Data can be queried as far back as it was recorded, but every page must be followed through `nextPageToken`.
- Most list endpoints allow up to 10,000 points per page; `sleep` and `exercise` are capped at 25.
- Endpoint type names use kebab case while filter message fields use snake case.
- Google Health OAuth scopes are Restricted. A personal project still needs the Google configuration and consent flow required by Google; broader distribution can require Google's verification/security review.
- Subscriber endpoints must be publicly reachable over HTTPS with TLS 1.2+.
- Subscriber creation performs an authenticated success handshake and an unauthenticated rejection challenge.
- Notification payloads are signed using a Tink-prefixed ECDSA P-256/SHA-256 signature. Google rotates keys and publishes the public keyset at a stable `gstatic.com` URL.
- Notifications can be retried for up to seven days, so periodic reconciliation remains necessary.
- As of 11 November 2026, the API does not provide a schema-enumeration endpoint. A versioned registry based on the official data-type table covers list, reconciled-stream, and rollup-only types; `EXTRA_DATA_TYPES` permits additions while the API evolves, while `ENABLED_DATA_TYPES` can deliberately limit consent and synchronization.
- The built-in registry includes every data type with a current read operation. Resource-schema types that are write-only as of 11 November 2026 (`menstrual-period`, `moods`, `ovulation-test`, and `symptoms`) are deliberately excluded so the mirror never requests write scopes or calls write endpoints.
- Read-only food and food-measurement-unit catalogs have no time filter. They are fetched once per backfill run, paginated, and retained raw like every other source resource.

## Historical backfill

**Starts a persisted backfill through the local API:**

```bash
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - -X POST -H 'X-HealthOS-Request: 1' \
  'http://127.0.0.1:8000/backfill/start?data_type=steps'
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - http://127.0.0.1:8000/backfill/status
```

**Starts a persisted backfill and waits:**

```bash
healthos backfill --data-type steps
healthos backfill
healthos backfill --reset-abandoned   # requeue abandoned windows, then run
```

One backfill runs per selection (a data type, or all types). A `POST /backfill/start` while the same selection is running joins that run instead of starting another, so a burst of requests spends one run's page budget; the answer is the same `202`, and a request after the run has finished starts a new one. All types and a single type are separate selections and can run side by side.

The default attempts history from 2010 in 31-day windows. Empty windows are valid and become completed checkpoints. Recent windows run first. On interruption, rerunning resumes the saved window and `pageToken`; completed windows are skipped. A window that keeps failing is moved behind the others after five failures and marked `abandoned` after ten, so it cannot stall its data type; see the [runbook](../RUNBOOK.md#abandoned-backfill-windows). Use a conservative start date to maximize completeness—the official API permits queries back to the earliest recorded data.

The `food` and `food-measurement-unit` resource catalogs remain discoverable but are not included in automatic mirror jobs. Google returns the global public food reference catalog through these unfiltered types; it is not personal health history. Personal `nutrition-log` records remain fully mirrored. Any reference rows already fetched remain preserved in the raw archive.

## Incremental sync

**Writes retrieved records to the configured database:**

```bash
healthos sync sleep
healthos sync
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - -X POST -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/sleep
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - -X POST -H 'X-HealthOS-Request: 1' http://127.0.0.1:8000/sync/all
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - http://127.0.0.1:8000/sync/status
```

Default overlaps are 60 minutes for heart rate, 12 hours for steps/activity streams, 72 hours for sleep/temperature derivations, seven days for daily metrics, and 24 hours otherwise. UPSERT makes every replay safe; a changed payload keeps the previous one as a [version](identity-and-versions.md#raw-record-versions).

### Source deletions

Google's list responses carry no tombstones; a deleted record simply stops appearing. When a window
was fetched from its first page to its last (an incremental run, a webhook re-fetch, or a
reconciliation), archived records that the same filter must have matched but that were not returned
get `source_deleted_at`. Nothing is deleted; dashboards and Umay skip marked records, and a record
that comes back is unmarked. The marked range is derived from exactly the bounds sent to Google
(UTC, whole seconds); rollup and reconcile types (aggregates) and catalogs without a time filter are
never marked.

- Session filters use civil start times (sleep: physical end time, exclusive). A webhook re-fetch
  of one sleep or exercise session therefore widens its window (sleep: one second past the
  session's end; other sessions: 12 hours before and 14 hours after, the range of UTC offsets) so
  the deleted session is inside both the query and the marked range. Outside webhooks, a
  scheduled window only marks sessions that started at least 12 hours after its start and 14 hours
  before its end; the rest is covered by the daily 7-day reconciliation.
- A round that set a record apart as unstorable ([records jsonb cannot hold](data-safety.md#records-jsonb-cannot-hold))
  marks nothing; the next round of the window without one does.
- A record whose time was moved upstream out of the window that is re-fetched looks deleted from
  that window and is marked until a fetch covering its new time returns it again (and unmarks it).
  For a move of more than 7 days that is the weekly 90-day reconciliation.
- "Not returned" is decided per [record identity](identity-and-versions.md#record-identity), not per row. A row is not
  marked while the same fetch returned its v2 key **with the same reading** under **the same
  device** (its own fingerprint, or one joined to it by a live chain: a duplicate of the record
  under another id; every link in force joins two such rows) - for a growing interval, either
  by a row it is hidden behind (a link in force) with the same end or a later one (the period
  grew, under either form, however the two forms were linked meanwhile: its start is returned,
  so it is not gone), or with the same end by another fingerprint of its device. A growing row
  that would otherwise count beside the end returned now is marked: a longer end than the one
  returned (stale; it would hide the one Google returns), an end its device returned under
  another form while nothing hides this row, or a copy its own fingerprint holds in another row
  (written while this one was hidden behind another end). Otherwise a row whose reading the
  fetch returned under a fingerprint that is not *certainly* another device is *deferred*
  (`SOURCE_DELETIONS_DEFERRED` in the log; one row per deferred raw row in
  `identity_deferred_deletions`, kept until a later complete fetch of a window that holds it
  decides it - marks it, keeps it, or sees it returned - so a fetch of another window never
  drops what an earlier one deferred; listed per pair of sources by `healthos status`
  under `identity_deferred_deletions` and by every reconciler run under
  `deferred_deletions`), never guessed - also when the row's own device returned another value,
  since the chain that makes it one device may be the wrong one. *Certainly another device*: the
  owner decided the two `distinct` (a form of each, joined to it only by chains the owner linked
  or confirmed), or the two fingerprints themselves are established distinct by evidence while
  their sightings do not merely touch and the keys they share do not mostly agree - judged on
  every key both hold, all counted (below), never on part of them - and the keys
  this fetch returned under the other fingerprint did not mostly come back with the rows'
  readings (records reprocessed under a new name). A mark is permanent, so it is never made on the
  strength of evidence the owner may still overturn. Otherwise (not returned at all, or only with
  another value by its own device, or the same reading only by a source certainly another device)
  the row is marked: of two devices that recorded at one instant, one can be deleted alone. Only a fetch from page 1 marks (a resumed fetch does not,
  whatever its pages returned). Superseded rows are candidates too (a superseded row counts again
  when its holder is marked, so it must be marked itself when its record is gone), except one
  whose holder this fetch saw unchanged since they were linked with the same reading: that is the
  holder's record, and it is not read again on every complete fetch. Candidates are read, decided
  and marked in chunks of 2,000, so a long window is never held in memory; whether a chunk's rows
  are held that way is looked up for its ids (by the links' primary key), so the work grows with
  the window, never with its square. A row another
  transaction holds (a page writing it, so it is being seen) is skipped (`SKIP LOCKED`), never
  waited for, so marking and a page never wait for each other in a circle; a deferral of a
  skipped row stays open, since nothing was decided about it. Deferrals are written under a
  per-data-type transaction lock taken only when free: while another complete fetch of the data
  type is writing them (until it commits), a fetch writes none (`SOURCE_DELETIONS_DEFERRALS_BUSY`)
  rather than queue behind it; the next complete fetch of the window records them. Marking records
  nothing else (no evidence of two devices: that comes only from Google responses). A deletion
  deferred between two sources is decided by the next complete fetch of its window after the
  owner's or the reconciler's decision (the decision fetches nothing itself); a pair the owner or
  the reconciler joined into one device is no longer listed at once, and the next complete fetch
  of the data type, of any window, resolves its deferrals.
