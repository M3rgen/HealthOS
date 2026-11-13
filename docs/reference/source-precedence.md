# HealthOS reference — Source precedence

Part of the [technical reference](../REFERENCE.md).

## Source precedence

When two sources report the same data, the interface shows one and keeps the other visible;
nothing is summed or averaged across sources, so nothing is counted twice. The rule is the
maintainer's decision of 7 November 2026, reviewed after a week:

| Category | What it holds | Default order (`.env`) |
|---|---|---|
| `activity` | workouts (`exercise_sessions`, Garmin activities, Fitbit exercises) and the heart rate, calories and distance measured *inside* a selected workout window | `SOURCE_PRECEDENCE_ACTIVITY=GARMIN,FITBIT,HEALTH_KIT` |
| `health` | everything else: daily steps, sleep, resting heart rate, HRV, SpO₂, continuous heart rate outside a workout, stress, body battery, weight and the other body measurements | `SOURCE_PRECEDENCE_HEALTH=FITBIT,GARMIN,HEALTH_KIT` |

A *source code* is the device platform a record came from, whatever delivered it: Google
records carry `dataSource.platform` (`FITBIT`, `HEALTH_KIT`, `HEALTH_CONNECT`; `GOOGLE` when it is
missing or a value was made from records of several platforms), Garmin rows are `GARMIN`, Apple
Health rows imported directly `HEALTH_KIT` (provider `APPLE`, [Apple Health](apple.md#apple-health-github-version)),
owner-entered rows `MANUAL`. A source the policy does not list ranks after every listed one
(`GOOGLE` right after them). **To reverse a rule, reorder its line** (`GARMIN,FITBIT` →
`FITBIT,GARMIN`) and restart; the stored rows do not change. Manual entries are never resolved
against device records: the owner typed them, they are shown as they are.

A *conflict* is the same metric from two sources over an overlapping time: the same calendar
day for a daily value, the same minute for a sample, overlapping start/end for a session
(`app/services/identities/source_precedence.py`: `resolve`, `resolve_samples`, `resolve_sessions`). The
observations of a conflict are ordered by the category's list; the first is *selected*, the rest
are *alternatives*, and all of them are returned:

- `GET /api/dashboard` and Umay's `health_archive daily`: each day's `steps`, `sleep_seconds`,
  `resting_bpm`, `hrv_ms` and `spo2` hold the selected source's value (a day only Garmin
  measured shows Garmin's), and `sources[metric]` gives `source`, `label` and `alternatives`
  (`[{source, label, value}]`). The hourly heart chart takes, per minute, the health policy's
  source outside and the activity policy's source inside a Garmin workout window
  (`charts.sources.heart_rate`: `source`, `alternatives`, `activity_windows`,
  `minutes_by_source`). The response carries the policy as `precedence`.
- `GET /api/dashboard/workouts`: every row has `source` (the label: Fitbit, Garmin, Apple Sağlık,
  Manuel), `source_code`, `provider`, `priority` (`primary`/`secondary`), `group` (on a secondary
  row: the id of the primary it overlaps) and `alternatives` (on a primary row: the overlapping
  secondary sessions). A Fitbit auto-detected walk inside a Garmin run is the run's alternative;
  a reversed policy makes the Fitbit session primary.
- `GET /api/devices` and Umay's `devices`: every device has `source_code`, `provider` and
  `precedence` (`{"activity": primary|secondary|unranked, "health": …}`); the response carries
  `precedence` (the policy) and `garmin` (`ok` when the archive holds Garmin records, else `off`).
- `GET /api/garmin/daily`: `precedence` per metric and `policy`.

The interface shows the primary source as a text badge (never colour alone) and the secondary
sources in a collapsed row or "diğer kaynaklar"; the **Cihazlarım** entry of every device says in
which category it is primary. The policy is read from the configuration on every request; tests
pass a `Policy` explicitly.
