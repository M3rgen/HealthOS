# Garmin FIT tracks / Garmin FIT izleri

Migration `20261115_0039` adds only `garmin_tracks`; `20261115_0040` adds the workout links
(`garmin_track_sessions`). No existing table is changed. The original FIT is authoritative;
packages can always be derived again from it. The [track API](#track-api) serves them; the
interface and maps are outside these changes.

## Storage / Saklama

`garmin_tracks` is a disposable read model, like `google_heart_hours`: no append-only
guard, and a downgrade drops it even with rows (`DISPOSABLE_TABLES` in
`app/db/downgrade_guard.py`). The originals it is derived from
(`garmin_activity_files`, manual FIT rows in `raw_health_data`) are never touched.

One package per distinct FIT content and `method_version`, keyed by
`(source_sha256, method_version)`. `source_sha256` is the SHA-256 of the FIT bytes, the
same digest as `garmin_activity_files.sha256` and a manual upload's
`raw_json["garmin-fit-file"]["sha256"]`, so a file that was both downloaded and uploaded
derives once. A changed file (new archive version) has a new digest and so a new
package; a version that returns (A → B → A) reuses its own. Failed binary decoding adds
an `invalid` package with empty columns, so the file is not retried.

The activity type is not stored: readers take running/walking/cycling from the existing
workout record. A package holds no name, device or place.

A package is three zlib-compressed JSON objects of equal-length columns (one array per
field, `null` for a missing value), a small JSON summary and the sample count:

| Column set | Columns (omitted when the file has no valid value for it) |
| --- | --- |
| `samples` | `time`, `latitude`, `longitude`, `altitude`, `speed`, `heart_rate`, `distance`, `cadence`, `power`, `temperature` |
| `laps` | `start_time`, `time` (lap end), `elapsed_seconds`, `moving_seconds`, `distance`, `average_speed`, `maximum_speed`, `average_heart_rate`, `maximum_heart_rate` |
| `events` | `time`, `action` (`pause` or `resume`) |

`time` columns are always present. Every time is whole seconds relative to the
summary's `start_time` (ISO 8601 UTC of the first valid record), so a reader can pick
columns and indices for thinning without row objects. A row per sample would repeat the
input key and index entries hundreds of times per activity.

Measured with seed 1 of the synthetic generator (not a real workout):

| Records | FIT bytes | Sample JSON bytes | Three compressed column sets, bytes |
| --- | --- | --- | --- |
| 700 | 21,201 | 36,246 | 9,789 |
| 20,000 | 600,201 | 1,099,061 | 253,174 |

Thus 10,000 synthetic activities of 700 records use about 98 MB of package payload,
plus summaries, row/index overhead and the separate original archive. This is a
payload estimate, not a PostgreSQL disk measurement; real records compress differently.
Derivation keeps 8-byte float columns (about 1.6 MB for 20,000 records) and streams
JSON into zlib. The 20,000-record allocation test measures about 2.5 MB peak beyond the
input bytes; it enforces a 4 MiB ceiling (decoded row dictionaries alone would need
about 10 MB). FIT input and message/record budgets still apply.

## Method 1 / Yöntem 1

- On the wire, times are UTC seconds since the FIT epoch (1989-12-31). Coordinates
  convert with `semicircles * 180 / 2**31` and are rounded to 7 decimals (about 1 cm,
  the semicircle resolution).
- Speed is m/s, distance and altitude are meters (altitude `raw / 5 - 500`), heart rate
  bpm, cadence rpm, power watts and temperature °C. Valid enhanced speed/altitude take
  precedence; missing or invalid enhanced values fall back to valid legacy fields.
- Unknown native/developer fields and messages are consumed without interpretation.
  Invalid sentinels, nonfinite values and invalid ranges (|latitude| > 90,
  |longitude| > 180, negative speed or distance) omit that measurement only; a
  missing/invalid timestamp omits its record. Other channels and neighbors survive.
  Duplicate/backward record times cannot add samples, time or distance.
- A position is kept only as a complete pair. The exact wire pair (0, 0) is dropped as
  "no fix", which some recorders write instead of the invalid sentinel; every other
  position near 0°/0°, the synthetic generator's region, is kept.
- Elapsed duration spans the first/last valid record. Moving duration is timer
  duration: timer-active seconds in that interval, subtracting explicit timer stop and
  resume events, including events between sparse records. Without timer-stop evidence
  the entire interval counts; it is not a speed-threshold estimate. A late (backward)
  timer event changes the state from the current clock and never turns it back.
- Distance sums nonnegative differences of the FIT odometer (`distance`; first reading is
  the baseline; resets add no jump; it already excludes pauses). Without adjacent
  odometer readings an interval uses the haversine distance on a 6,371 km sphere
  (within 0.5 % of the ellipsoid; straight chords undercount curves between sparse
  records, 1.2 % at 25 s on the test circle), then trapezoidal speed over active
  seconds. Paused fallback intervals add nothing.
- Average speed is distance/moving duration; maximum speed uses valid speed samples.
  Climb/descent accumulate changes of at least 3 m from the last accepted elevation
  anchor. Small changes must accumulate past that threshold; no trailing noise is added.
  Bounds are `[minimum latitude, minimum longitude, maximum latitude, maximum longitude]`.
  Speeds, climb/descent and bounds are null without the channel.

## Rebuild / Yeniden türetme

```sh
python -m app.cli.garmin_tracks --rebuild
```

New downloads and uploads derive through the same service inside their archive write,
under a savepoint: a projection failure is logged as `GARMIN_TRACK_DEFERRED` (failure
type only) and never undoes the archived original. The command fills missing packages
for the current method, including older uploads and deferred files, and the workout links
of manual uploads (`garmin_track_sessions`, see [track API](#track-api)). Each input commits
separately; interruption rolls back only that input and a restart skips completed
packages. An unreadable legacy upload is skipped (`GARMIN_TRACK_IMPORT_UNREADABLE`)
without blocking the rest. A new method version adds new packages; old ones stay until
maintenance removes them. Repeating the command is idempotent and derivation of
identical bytes is deterministic. It prints only the number of newly completed packages
(including invalid ones), or `0` and a nonzero exit status on failure. It never prints
coordinates, filenames or exception contents. No new service, setting or dependency is
required.

The generator in `tests/fit_factory.py` follows a seeded circle of radius 0.005° around
0°/0° in the open ocean (`TRACK_BOUNDS`); tests fix these bounds and that the exact pair
(0, 0) never occurs. No real location is included.

## Track API

`GET /api/dashboard/workouts/{workout_id}/track?max_points=<n>` returns one workout's track.
It is a dashboard read like the others: a login session or bearer token is required (`401`
otherwise), the response is `Cache-Control: no-store`, the read runs under
`API_READ_STATEMENT_TIMEOUT_SECONDS` and a cancelled read answers `504` with
`{"detail": "Database read timed out. Please try again."}`.
The interface draws it in the workout detail view ([Workout map](maps.md)), asking for
`max_points=2000`.

### Which workout, which file

`workout_id` is the `id` of a row of `GET /api/dashboard/workouts` (a UUID; a malformed one
is `422`). Each list row carries `has_track`; it is `true` exactly when this endpoint
returns a track for that `id`.

| Answer | When |
| --- | --- |
| `404` `{"detail": "Antrenman bulunamadı / Workout not found"}` | No FIT-backed workout (Garmin, or another FIT manufacturer's upload) with this id is listed: an unknown id, a manual or Google workout, an older version or a quarantined workout. The same visibility rules as the list apply. |
| `200` `{"track": null}` | A listed FIT-backed workout without a ready package: the original file is still pending or unavailable at Garmin, it was refused (`invalid`), it could not be decoded, it has no sample, or packages were not rebuilt yet after an upgrade (`--rebuild`). |
| `200` `{"track": {…}}` | Otherwise. |

A Garmin API workout (`activities:<key>`) uses its activity's current original file: the
newest `downloaded` result in `garmin_activity_file_results`, so a new file version replaces
the track. A manual upload's workout (`fit:<record key>`) does not carry its file digest;
the import, and `--rebuild` for earlier uploads, records which content produced it in
`garmin_track_sessions` by parsing the archived upload again. If several uploads produced
the same workout, the most recently received upload is used. A file holding several sessions
(multisport) returns its whole track for each of them. The activity type comes from the
workout row (`exercise_type`), never from the package.

### Body

```json
{"track": {
  "workout_id": "<the requested id>",
  "exercise_type": "<the list row's type or null>",
  "method_version": 1,
  "summary": {"start_time": "<ISO 8601 UTC>", "elapsed_seconds": 0, "moving_seconds": 0,
              "distance": 0, "average_speed": null, "maximum_speed": null,
              "ascent": null, "descent": null, "bounds": null},
  "source_point_count": 0,
  "point_count": 0,
  "samples": {"time": [], "latitude": [], "longitude": [], "…": []},
  "laps": {"start_time": [], "time": [], "…": [], "start_index": [], "end_index": []},
  "events": {"time": [], "action": []}
}}
```

Every object of arrays is columnar: all its arrays have the same length, `null` marks a row
without that value, and a channel without any value in the file is absent (no empty
series). `point_count` is the length of the `samples` arrays; `source_point_count` the
stored sample count.

| Field | Unit |
| --- | --- |
| `summary.start_time` | ISO 8601 UTC of the first valid record; every `time`/`start_time` below is whole seconds relative to it |
| `summary.elapsed_seconds`, `moving_seconds` | s (moving = timer running, see method 1) |
| `summary.distance`, `ascent`, `descent` | m (`ascent`/`descent` null without altitude) |
| `summary.average_speed`, `maximum_speed` | m/s (null without speed or moving time) |
| `summary.bounds` | `[south, west, north, east]` in degrees, null without positions |
| `samples.time` | s, relative, strictly increasing |
| `samples.latitude`, `longitude` | degrees, 7 decimals |
| `samples.altitude`, `distance` | m (`distance` is the FIT odometer) |
| `samples.speed` | m/s |
| `samples.heart_rate` | bpm |
| `samples.cadence`, `power`, `temperature` | rpm, W, °C |
| `laps.start_time`, `laps.time` | s, relative: lap start and lap end |
| `laps.elapsed_seconds`, `moving_seconds` | s |
| `laps.distance` | m |
| `laps.average_speed`, `maximum_speed` | m/s |
| `laps.average_heart_rate`, `maximum_heart_rate` | bpm |
| `laps.start_index`, `end_index` | position in the returned `samples` arrays of the first sample at or after the lap start/end (null when the lap time is missing) |
| `events.time`, `events.action` | s, relative; `pause` or `resume` (timer) |

Pace (min/km), the time or distance axis and colours are the interface's choice; the API
sends SI values only.

### Thinning (`max_points`)

`max_points` is optional, an integer from 100 to 50,000 (`MAX_FIT_RECORDS`); without it every
sample is sent. When the track has more samples, at most `max_points` rows are kept and every
`samples` array keeps exactly those rows, so columns stay aligned:

- The first and last sample and every lap boundary sample are always kept. If lap
  boundaries alone exceed the budget, they are spread evenly instead.
- The rest of the budget is shared equally by the route (longitude, latitude) and each chart
  channel against time (altitude, speed, heart rate, cadence, power, temperature; only those
  present). Each runs Largest-Triangle-Three-Buckets: the rows are cut into equal buckets and
  each bucket keeps the row that forms the largest triangle with the previously kept row and
  the next bucket's mean, which keeps corners, peaks and spikes. Rows missing that channel
  are skipped for it. A track with no channel is spread evenly.
- The kept rows are the union of these picks. The result is deterministic (ties keep the
  earlier row), and the same request returns the same bytes.

Douglas–Peucker is not used: it needs the whole track in memory and recursion over it.
LTTB here reads the stored columns twice in a stream (bucket means, then picks), so memory
grows with `max_points`, not with the sample count.

### Size and memory

The response is produced after the database read ends and streams in pieces of at most a few
KiB. Packages hold at most 50,000 records (samples, laps and events together), and every
value is at most about 25 bytes of JSON, so a body without `max_points` stays below about
13 MB; with `max_points` the samples stay below `max_points × 10 × 25` bytes plus the laps.
Measured with the synthetic generator (seed 1, not a real workout):

| Records | Body, all samples | `max_points=2000` | `max_points=500` |
| --- | --- | --- | --- |
| 700 | 36,900 B | 36,900 B | 12,935 B |
| 20,000 | 1,099,731 B | 59,512 B | 15,570 B |

Four queries answer any request (the workout row, its listing, the file or link and the
package), independent of the sample count; no file body is read. In the workout list,
`has_track` adds at most three queries per 500 FIT-backed rows of a provider. Planning and streaming a
20,000-record track stay below the 2 MiB allocation ceiling of
`tests/test_garmin_track_reads.py`, with or without `max_points`, while decoding its samples
into Python lists alone exceeds it. Thinning runs in a worker thread. The endpoint writes no
log line; error messages are fixed and never contain a coordinate, a sample value or a name.
