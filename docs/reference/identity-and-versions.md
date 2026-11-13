# HealthOS reference — Record identity and versions

Part of the [technical reference](../REFERENCE.md).

## Record identity

Records with a Google `name` are identified by it. Most data types (heart rate, steps, zones,
activity level, intervals, daily summaries, rollups) arrive without one; until migration
`20261021_0012` they were keyed by a hash of the raw `interval`/`sampleTime`/`date` object, the
zone fields, the **whole** `dataSource` and the top-level start/end (v1, `synthetic:<hash>`). Each
of these upstream changes therefore made the same record a new row: Google renaming a device
(`dataSource.device.displayName`), Apple Health dropping the `.<UUID>` suffix of its package name,
Google adding civil times and UTC offsets inside `interval`, and a sedentary period whose end grows
every 15 minutes while its start stays.

The **v2 identity** (`app/parsers/identity.py`) hashes only:

- the data type;
- the physical UTC start and end (intervals; rollups: their top-level window) or the instant
  (samples; daily records: their civil date), as parsed, never the raw time objects, so civil
  times and offsets do not count;
- for **growing intervals** the start alone. Only `sedentary-period` is known to behave like that
  (in the archives examined, no other type had two records with the same start, source and
  discriminators but different ends; sleep and exercise carry Google ids);
- the discriminators `heartRateZone`, `heartRateZoneType`, `activityLevelType`,
  `measurementLocation`, `measurementMethod`, `swimStrokeType`;
- the stable source fields `platform`, `recordingMethod`, `device.formFactor`,
  `device.manufacturer` and `application.packageName` without a trailing `.<UUID>`.

Left out (but kept in the record's *fingerprint*): `displayName` and every other `dataSource`
field, and a growing interval's end. The value never belongs to the identity; a changed value of
the same identity is a new [version](#raw-record-versions) of the same row.

`device.formFactor` and `device.manufacturer` are **optional device fields**: Google may add one
to a source's records that it always sent without (it began sending `formFactor:
"FITNESS_BAND"` for a band known until then by its name alone, for some data types without the
name, and not for every record). They stay in the key, so no stored key or row id changes, but
rows of two fingerprints are compared by their **match key** (the key without them; the key
itself for a source that has none). Two fingerprints are **kindred** when they are of one stable
class (a rename, as always), or when their `dataSource` is the same but for device fields one has
and the other lacks: an optional device field, perhaps with the `displayName`. A form with the
field added is then a new form of the source, like a rename: the deletion marking defers on it,
the reconciler chains it and links its rows, the owner can decide it. A source with another
name *and* the field, or a phone and a watch, is not kindred: never compared, never in the way of
a chain, exactly as before. Kindred is not transitive (a source without `formFactor` is kindred
to a phone and to a watch of its name), so a chain or an owner's link is only ever made between
two devices whose forms are **compatible** two by two (no optional device field with two
values), and no supersession ever joins two incompatible rows: a device is never a phone and a
watch. In data without optional device fields nothing of this changes anything; the
reconciler's `identities` and `distinct_sources` count per match key (equal to the key there).

**Renames are decided per source, by the reconciler or the owner.** Fitbit sends no serial
number, only `displayName`, and Apple Health sometimes differs only by the package's `.<UUID>`
suffix, so a renamed device and a second device recording at the same instant look alike in one
record. The question is therefore asked of the *sources*, per data type and group of kindred
fingerprints (without optional device fields: exactly one stable class, whose fingerprints are
the only ones that can share a key).

*Evidence of two devices* (`raw_source_pairs`): a key that **one Google response** returned under
two fingerprints **with two different readings**, noted with when its fetch began. One record has
one value: a renamed device (or one whose old name comes back) never returns two values for one
record, two devices that measured one instant do. Two fingerprints are **established distinct**
only by such evidence for three different keys (`PAIR_KEYS`) **from at least two fetches**, a later
one returning such a key for a record **newer than the first fetch** (both sources still produce
new records side by side). One response is never enough, however many keys it holds: while Google
switches a name it can return a batch of old records in both forms with values one off; nor are
old records returned again in both forms. With equal readings a co-returned key is no evidence at
all. The write path records it per page, at most three keys per fetch (the newest first) and only
while they can still help (a pair that cannot be established by a key gains no more); a full
`identity-backfill` derives it from rows of one key that one page stored or last saw together
(equal `first_seen_at` or `last_seen_at`: one transaction's clock). Rows merely seen minutes apart
are no evidence (a rename in the middle of a fetch looks exactly like that). The owner's
`distinct` / `not-distinct` decision overrides the evidence. Distinctness holds between
**devices**: a source distinct from one form of a device is distinct from all its forms. A pair
established distinct by evidence alone whose sightings only touch (one began within a day of the
other's end, `RENAME_OVERLAP`) looks like a rename: every run's report and `healthos status`
list it under `review_distinct` until the owner confirms it (`distinct`) or decides
`not-distinct` and links, and no deletion is marked on its strength.

The chain rule works on every fingerprint's sighting span in `raw_source_sightings` (first and
last time a record carrying it was returned, stamped with the **fetch time of the response**, so
a page that waited for a lock is still seen when Google returned it).
`app/services/identities/source_chains.py` links *A* → *B* in a device's **chain** (`raw_source_chains`) only
when

1. every fingerprint of the class has a complete span (built from the whole archive by a full
   `identity-backfill`, or first seen after that);
2. *B* was first seen **more than 5 minutes** after *A* was last seen (`CHAIN_GAP`);
3. *A* is *B*'s only predecessor candidate (last seen before *B* appeared, with nothing of the
   class appearing in between) and *B* is *A*'s only successor candidate (first to appear after
   *A* ended);
4. no other fingerprint of the class was first seen in between, or was seen **alongside** either
   *A* or *B* (overlapping spans: a second device of the class makes any rename near it undecided);
5. *A* and *B* are not established distinct, neither already has a live link the other way (a
   chain never forks; no cycles: *A* → *B* → *A* is never a chain), and no link between them was
   rejected or refuted (the owner decides that pair again).

A fingerprint **established distinct from** *A* or *B* and with evidence for three keys against
the other (from any responses: a second watch worn alongside the band, before and after its
rename, whose evidence against the new name may come from one re-fetch of old records) is left
out of rules 3 and 4: it is known to be another device, so it says nothing about *A* → *B* (a
chain merges equal readings only, and no deletion is marked on that weaker evidence). One established distinct from *B* is never *B*'s
candidate. Nor is one that **retired before** *A*: established distinct from *A* and with no record
newer than seven days before *A*'s newest record and *B*'s first sighting (the sightings keep the
physical time of each fingerprint's newest record, `newest_record_at`, which a re-fetch of old
records does not move): it stopped recording while *A* went on for days, so it is neither *B*'s
earlier form beside *A* nor seen alongside *B* when Google returns its old records again (a
second watch retired before the band was renamed). A device that kept recording until the rename,
or was only left unworn for hours or a few days (a watch on its charger), still blocks it: *B*
may be that device renamed.

A three-step rename *A* → *B* → *C* chains step by step even when *B* only ever returned new
instants and never met *A*'s rows. Everything else is **ambiguous**: nothing is linked, both
sources keep their rows (visible, at worst counted twice, never hidden), and every run's report
and `healthos status` list the case under `ambiguous` with its reason (`a source of its class
ended less than five minutes before`, `more than one source of its class ended before it`,
`another source of its class appeared in between`, `another source of its class was seen
alongside`, `history incomplete`, …) until the owner decides it (`identity-decide`, below). A
typical device rename leaves hours between the old name's last and the new name's first sighting;
a rename between two pages of one fetch (minutes) is left to the owner.

A link joins its two fingerprints into **one device in both directions** and records the spans it
was decided on as `evidence`. The old form coming back later is no contradiction (a name can
change back): its records get rows of their own again, linked to the device's (`returned` in the report). A link is
**live** until it is **refuted** (its two fingerprints became established distinct: a `refuted`
event in `raw_source_chain_events`, recorded by the reconciler), the owner decides the two
`distinct`, or the owner's latest decision about it is `reject`; a later `not-distinct`, `link` or
`confirm` makes it live again. Only live links make devices, and only live links must form chains
(one successor and one predecessor per fingerprint, no cycle: checked when a link is recorded or
made live again, and by `verify`); a rejected or refuted link frees both its fingerprints for
another link. Whenever devices change, the device of every chained fingerprint is appended to
`raw_source_devices` (the latest row per fingerprint is current). A supersession records both
rows' fingerprints and is **in force** only while they are one, or one device there: readers
(dashboards, Umay, the `analytics_*` views, `held_elsewhere`) skip a superseded row only through
a link in force, so a wrong link is undone in what they count the moment it is rejected or
refuted, and done again by the opposite decision. `verify` notes links no longer in force and
checks the device log against the chains. Every link the reconciler decides is listed for
**review** (report, `healthos status`) until the owner confirms or rejects it.

**Writing a record** (`store_records`, `app/services/identities/identity.py`). The write path never decides a
rename; it applies decided chains (a *device* is a fingerprint and every one joined to it by usable
links, either way). **A payload is only ever written onto a row of its own fingerprint**, so a row's
source never changes and no row's history holds another source's payload (`verify` checks it).
De-duplication across fingerprints is a matter of links alone. A row is *hidden* exactly when
readers skip it: a link in force to a row neither marked deleted nor quarantined. Records of one
page are grouped by v2 key and fingerprint, and every group is written onto

1. a **row of its own fingerprint that leaves its reading counted**: one not hidden, or one whose
   hiders show the same **reading** (the payload without the source, times as parsed; for a
   growing interval also the same end - neither an earlier nor a later one); of several, the one
   counted, then live, then already holding this payload, then not superseded by a row of its
   own fingerprint, then the one seen last: a returning record, civil times added, a revised
   value, the old name back (a deletion mark on it is cleared). A spare row of its own that
   fits is reused like any other, so a record that flaps between names or values cycles through
   a few rows instead of adding one each time;
2. else a **new row of its own**, `synthetic:v2s:<hash of key and fingerprint>` (the first free
   spare id of the same kind if that id is taken): a reading Google returns is never hidden
   behind a row showing another value.

A row the page makes **counted** - a new row, or one marked deleted until now - **holds at once**
every counted row of another fingerprint of its device that shows the same reading (a
supersession and its `identity-merged` event, written by the page): a decided rename, a dropped
package suffix or the old name come back is counted once from its first page, and an undecided one
at worst twice. The row Google returned now holds (a later value under its name is written onto
it instead of a new row hidden behind a stale one); of two rows the page writes, the device's
newest form (by its first sighting). A growing interval holds only a row with the same end: a row
showing another end is a value Google no longer returns, counted beside it (at worst twice) until
the complete fetch marks it (above), and a link would keep the row of its own hidden when that
end comes back. A link that would close a cycle of links is left out (both count; the reconciler
lists it). Rows are found by id only: the record's v1 id (rows archived before v2), the `v2s` id of its key
and fingerprint (rows stored since 0012) and its alias, and the `v2s` ids and aliases of the
device's other fingerprints, in one statement; their links (any state, and the links of the rows
those point at) in the next; when no row of its own fits while its `v2s` id is taken, its spare
ids, in blocks of eight until one is free (spare ids are given in order, so the spare rows written
before are found again), and the choice is made again with them. There is no "newest row" fallback and no
scan by start time: an unknown fingerprint (a rename not decided yet, a second device, a late
upload) is counted twice at worst until it is decided, never hidden. Concurrency: every existing
row the page relies on - the rows it writes onto, every row that could hide one of them (the
targets of their links, any state, transitively: a marked one may be revived meanwhile) and the
rows it links - is locked (`FOR UPDATE`, key order) and checked against what the resolution read
(payload, deletion mark, quarantine and links), and new rows are
inserted with `ON CONFLICT DO NOTHING`; when another writer changed, revived, linked or created one
meanwhile, the page is resolved again inside a savepoint (`RECORD_BATCH_RESOLVED_AGAIN`). The time written to
`last_seen_at` is read after the data type's lock was granted, and `last_seen_at` never moves
backwards (`GREATEST`), so a writer that waited cannot make a record look unseen to a fetch that
started meanwhile. The page's sightings (the one row per source that every writer of the data
type shares) and its evidence of two devices are written last, so that row lock is held for as
short as possible.

**Heart-rate zones.** v1 left `heartRateZoneType` out of the key, so the four zone records of one
interval (`LIGHT`, `MODERATE`, `VIGOROUS`, `PEAK`) were written onto one row, each overwriting the
previous one (before `20261020_0011` without a version). v2 keys each zone type apart: from the
deploy on, a re-fetch of such an interval keeps the stored row for the zone type it holds now and
adds rows for the other three. The backfill does nothing more: the stored row holds one real zone
record, its versions (since 0011) hold the others as they were received, and nothing is rewritten;
intervals never re-fetched keep showing one zone type of four.

**Aliases.** A raw row's `google_data_point_id` never changes (trigger), so existing rows are not
re-keyed. `raw_health_data_identity (data_type, identity_key → raw_id, google_data_point_id,
created_at)` points the fingerprint-specific key (`v2s:…`) of every row archived under a v1 id at
that row (the reconciler adds them; the write path adds one when it writes onto such a row), so a
record whose v1 id changed is found again. Rows stored since 0012 need none.

**The reconciler** (`healthos identity-backfill`, `app/services/identities/identity_backfill.py`) is the
only place that decides chains, and it links what it decided. A **full** run (after the deploy,
and whenever wanted) reads every row and earlier version (pass 1: sighting spans; rows whose
stored `source_start_time` differs from the parsed one are noted by id), merges the spans into
`raw_source_sightings` (complete from then on), decides the chains, then per v2 key and per device
(one fingerprint or one usable chain) links its rows in two steps: per fingerprint, every row not
yet superseded by one of its own to that fingerprint's **root** (live first, then the one seen
last; a growing interval: the end returned last, then the longest); then every counted root -
live: a row marked deleted counts nowhere, and a link would hide it if its record came back with
another value - to the device's **holder** among them (a growing interval: the end returned
last; then the one the others already link to, the one that already holds others, the one seen
last, the device's newest form), in `raw_health_data_supersessions
(old_raw_id, new_raw_id → data_type, both keys, reason, both fingerprints, same_reading, the
holder's payload hash, created_at)` with the reason `display-name-change`, `package-suffix-change`,
`source-change`, `interval-shape-change`, `growing-interval` or `duplicate`, and appends an
`identity-merged` event for the superseded row (once per row, ever). A root of **another
fingerprint** is linked only when it measured the same (its reading equals the holder's);
otherwise both stay (`kept_differing`). Only roots are linked across fingerprints, so when a chain
is rejected or refuted each fingerprint still counts each record once. The live rows a newly
linked row held are linked to the holder too (rows of the holder's fingerprint, and rows that
measured the same), so two devices joined later end with one holder. Rows whose links are no
longer in force are settled like any other and linked again within each device that remains.

**Agreement.** Per pair of fingerprints with evidence of two devices it also notes, **per key both
hold**, whether some row of each holds the same reading (`raw_source_agreements`, append-only and
guarded like the other identity tables: a row only when that changes, the latest per key is
current) and moves the pair's counts (`identity_reconcile_state.pair_agreement`) by exactly that,
in the same transaction: a key settled again by the overlap, a full run or a decision counts
once, and a night's sample of a few keys never replaces what the archive says. A pair's counts
are **complete** once every key both hold was counted: a full run counts every pair with evidence
when its second pass begins (and sums the counts again from the per-key rows), and the nightly run
first counts the shared keys of every pair established distinct whose counts are not complete (the
start times with rows of more than one source since the later of the two was first seen; reading
rows only, no data type lock). When most keys agree (at least three), the pair is no longer taken
for two devices on that evidence (a rename whose records Google reprocessed) and is listed under
`review_distinct`; a chain is **refuted**, and a deletion marked on that evidence, only when the
counts are complete. `verify` sums the per-key rows again and reports counts that differ. A deleted-marked row with no live row of its device is a deletion:
`deletions_kept` when no other source holds its key live, `deletions_suspicious` when another,
undecided source does (possibly a rename nobody decided: see `ambiguous`). The **incremental**
run (`--incremental`, the nightly scheduler job
`identity-reconcile`, started when the scheduled reconciliations end; see
[Reconciliation](reconciliation.md#reconciliation)) decides chains from the sightings as the write path
keeps them and links the start times of rows created or given a new payload (or whose deletion
mark was cleared) since its checkpoint minus one hour, plus exactly the rows whose devices changed:
every row of a link
between two fingerprints that are no longer one device (a refutation; whatever its age, also rows
archived before 0011), and every row first seen since the later of two fingerprints that newly are
was first seen (the start times are read first, in short transactions of their own, as integers;
no snapshot is held while they are settled). Which pairs of fingerprints are no longer, or newly,
one device is written to `identity_reconcile_state.pending_settle` in the transaction that
changes them (a refutation, a new chain, an owner decision) and cleared only when their rows were
settled: a run or decision that fails in between leaves it, and the next run (or decision)
settles it too; a dry run shows it among what it would settle (and writes nothing). A data type
that
never had a full run gets its first full pass instead and says so (`skipped`). A data type that
fails (its lock stays busy, a statement times out) is reported with its `error` and saved in
`identity_reconcile_state`; the run goes on with the others (the CLI then exits 1). Both report
the chains (with their state and review state), the chains awaiting review, the ambiguous sources,
the pairs kept apart (established distinct or not), the pairs that look like a rename although
evidence made them two devices (`review_distinct`), refuted chains, chains whose old form
returned and the rows left double because linking them would hide a live row behind a deleted
one or close a cycle (`conflicts`). A full run
marks every sighting of the data type complete (also a source first stored while it ran).
Checkpoints and the last result per data type, with an `open` section of everything left to
decide, are in `identity_reconcile_state` and in `healthos status` (`identity_reconcile`;
`identity_review_suggested` lists the chains to review).

**Deciding by hand** (`healthos identity-decide`, `app/services/identities/identity_decisions.py`).
`link FROM TO` (one device; records a chain with `decided_by = owner` naming the decision, or
makes a rejected chain live again), `reject FROM TO` (the chain is wrong: no longer used, its links
no longer hide anything, both sources are free for another chain), `confirm FROM TO` (reviewed; a
rejected chain is live again), `distinct A B` (two devices: a chain between them is no longer used)
and `not-distinct A B` (ignore the evidence of two devices; a chain refuted by it is live again).
Each decision is appended to `raw_source_decisions` (with its time and an optional note) and
applied at once: the rows whose devices it changed are settled again, exactly as the incremental
run settles them (the rows of every lapsed link; every row since the later of two sources newly
joined). A decision records only itself: a chain the rule would decide because of it (a rival
source now known to be another device) is not recorded inside the decision but listed under
`left_to_reconciler` in its outcome, and the next reconciler run decides it like any other (listed
for review), so undoing the decision never meets a chain it made. The latest decision per question
wins, so each decision is undone by the opposite one, in what readers count too (there is no
decision that forgets one). Exactly: `distinct` ↔ `not-distinct`, and `reject` ↔ `link` for a
chain the owner linked. For a chain the reconciler suggested, `confirm` after `reject` makes it
live again but **reviewed** (state `confirmed`, no longer `suggested`): it no longer depends on its
premise (below), and its forms count as reviewed for a `distinct` (deletion marking reaches them).
While the premise holds, readers count exactly as before the `reject`. If the reconciler linked
one of the two freed sources to another one meanwhile, `confirm` is refused, naming that chain;
rejecting it first and confirming makes the original state's counts again, with that chain left
rejected. A chain the
reconciler decided only because another live chain made a rival source continue elsewhere records
that *premise* in its `evidence`; it is live only while the premise is (state `lapsed` otherwise),
unless the owner linked or confirmed it, so undoing the other chain undoes it too. `distinct`
warns, in its outcome and its dry run, that rows of one source whose reading then comes back only
under the other are marked deleted by the next complete fetch: a mark is permanent. A decision that would make live chains fork or cycle
is refused, naming the chain to reject first (for a cycle: the live chains already joining the two,
the first of them to reject); `link` of two sources the owner decided distinct, or
established distinct by evidence, is refused until `not-distinct`. A decision waits up to a minute
for the single-run lock it shares with the reconciler (a nightly batch run), then is refused,
naming who holds it (`healthos-identity-backfill` or `healthos-identity-decide` as the session's
`application_name`) and since when. `--dry-run` reports the effect,
writes nothing and takes no lock that writers wait for; an unknown `--data-type` is named as such.
`healthos identity-sources --data-type <type>` lists the sources with their fingerprints,
spans, devices, chains, decisions and open cases. See [docs/RUNBOOK.md](../RUNBOOK.md).

A superseded row is **history, not data**: its payload, versions, `source_deleted_at` and events
stay exactly as they were, but while a link in force joins it to a live row, dashboards, Umay, the
`analytics_*` views and the normalized-table readers skip it, as they skip a source deletion, so a
record is counted once. When every such holder is marked deleted (or quarantined), a live
superseded row counts again: a link never hides a live record behind a row that is not counted.
With source deletions included (the dashboard's option), a deleted row is left out when it is
superseded or holds a live row that counts, so a live row's value is shown, never a deleted
holder's (a marked row of another form of the device, not linked to the live one, is shown beside
it in that view). Old deletion marks
are not cleared: they truthfully say that the row's form of the record stopped being returned on
that date; the record itself lives on in the successor row. A marked row with no live row of its
device is a genuine deletion and is never linked (`deletions_kept`), and no marked row is linked
to a row of another fingerprint. Rows of different devices are
never linked. A row may have several links (one no longer in force, then another holder); its
holder is the target of its newest link in force. No link closes a cycle of links (in any state;
`verify` checks it); a link that would is left out (`conflicts`). Rows with a stale stored start are settled last, with the rows of their parsed start
(`start_mismatches`), also when `--after` skipped the rest of the second pass.

The alias, supersession, chain, chain-event, pair, decision, device and agreement tables are
append-only (no `UPDATE`, `DELETE` or `TRUNCATE`, `ENABLE ALWAYS` triggers); `raw_source_sightings`
may only widen (no `DELETE` or `TRUNCATE`; an `UPDATE` may only move `first_seen_at` earlier,
`last_seen_at` and `newest_record_at` later and `complete` to true, never change its key, class,
label or `dataSource`). `healthos verify` checks them with the other guards, their keys and checks, that no
rule, row-level security or policy sits on a guarded table, that no guard sits on a table that is
not guarded (a guarded table renamed away), that every `identity-merged` event still has its link,
that the `analytics_*` views are the ones of 0013, and the links and chains themselves: see
[Integrity verification](data-safety.md#integrity-verification). `identity_reconcile_state`,
`identity_deferred_deletions` (one row per deferred deletion; never deleted, `resolved_at` set
when a later complete fetch decides it, or finds its two sources one device) and `fetch_rounds` (when a sync round or backfill window
fetch began at its first page; a resumed fetch keeps it) are bookkeeping.

The dashboard, its history and workouts, and Umay's reads plan every statement with its own
parameters (`plan_cache_mode = force_custom_plan` for their transaction): a generic plan, which
PostgreSQL switches a long-lived connection to after five executions, cannot use the time bounds
and reads many times more rows.

**Concurrency.** `store_records` and deletion marking take a shared transaction lock per data type,
each reconciler batch the exclusive one: it tries for it without queueing for 0.1 s, then waits in
the queue for at most 1 s (a waiting exclusive request queues every new writer behind it, so no
sync waits more than about a second for a batch that is still waiting, even behind a long
transaction); once it holds it, it waits at most 2 s for any row
lock of another transaction (a long import holding rows), then rolls back, waits 2, 4, 8 … s and
retries (`IDENTITY_RECONCILE_LOCK_BUSY`; the CLI prints each retry), and after 8 attempts gives up
on that data type clearly; a sync of that data type waits for one batch (seconds), then continues.
Any long writer of the data type holds it that way: the daily or weekly reconciliation, the
watchdog's recovery after a restart, a sync or backfill by hand. So a run does not give up yet: it
goes on with the other data types (`IDENTITY_RECONCILE_DATA_TYPE_DEFERRED`), then tries the busy
one again, its batches backing off (up to 60 s) until a deadline: the end of the nightly window
(`IDENTITY_RECONCILE_UNTIL_*`, 03:55 UTC) for the nightly job started before it, otherwise
`IDENTITY_RECONCILE_LOCK_WAIT_MINUTES` (60) from its start, also for `identity-backfill` by hand
(`0`: the 8 attempts alone; a `--dry-run` takes no lock). Each attempt keeps the limits above, so
the deadline bounds the retries, never a lock held longer. Only then is the data type failed:
`last_error` in `identity_reconcile_state`, `failed_nights` in `status` and
`identity_lock_contention` in `/health` (below).
A dry run takes no lock at all. Only one reconciler or decision runs at a time: a second
reconciler refuses at once (`another identity-backfill is running`, naming who holds the lock and
since when); the nightly job then skips; a decision waits up to a minute first. Inserts are
chunked (1,000 rows a statement, far below PostgreSQL's 32,767 parameters) for any `--batch-size`.

```sql
-- Superseded duplicates per data type and reason
SELECT data_type, reason, count(*) FROM raw_health_data_supersessions GROUP BY 1, 2 ORDER BY 1, 2;

-- Source chains (renamed devices, dropped package suffixes)
SELECT data_type, from_label, to_label, decided_by, evidence::json->>'gap_seconds' AS gap_s
FROM raw_source_chains ORDER BY data_type, decided_at;

-- Refuted chains (chains whose two forms turned out to be two devices; no longer used)
SELECT data_type, from_fingerprint, to_fingerprint, occurred_at FROM raw_source_chain_events;

-- Devices (each chained fingerprint's device now: what readers count supersessions by)
SELECT DISTINCT ON (data_type, fingerprint) data_type, fingerprint, device, decided_at
FROM raw_source_devices ORDER BY data_type, fingerprint, id DESC;

-- The owner's decisions (the latest per question wins)
SELECT data_type, decision, first_label, second_label, note, decided_at
FROM raw_source_decisions ORDER BY id;

-- The row that holds a duplicate's record now
SELECT s.reason, n.google_data_point_id, n.raw_json
FROM raw_health_data_supersessions s JOIN raw_health_data n ON n.id = s.new_raw_id
WHERE s.old_google_data_point_id = 'synthetic:…';
```

### Hollow Google names

A Google `name` whose data point id is empty (`users/…/dataTypes/<type>/dataPoints/`) names the
data type, not a record. `respiratory-rate-sleep-summary` arrives so: every sleep's summary under
one name. Such a record is identified by the name plus `@` and its own UTC time
(`app/parsers/common.py`, `source_id`): the start of its interval, or a sample's time. For a
sleep summary that time is the end of the sleep it describes (in the archive it equals a sleep
session's end, or falls inside a session that holds several summaries; one response returns
them all together, so they are separate records, not revisions). The identity is never a
calendar day: sleep by day after a night shift, sleep across midnight and two sleeps of one day
are each their own row; a revised summary of the same sleep is a new version of its row. A
hollow name without any time keeps the bare name.

Before this, every summary was written onto the one row of the bare name: that row holds the
last sleep it received and the earlier sleeps are its [versions](#raw-record-versions). It is
never written, deleted or rewritten. When the row of its own sleep holds exactly its payload,
the write path links the old row to it (`app/services/identities/hollow_names.py`; reason `hollow-name`,
one fingerprint on both sides, so always in force): readers count the sleep once, and a
complete fetch that returns the sleep does not mark the old row deleted. Without such a row
(Google no longer returns that sleep, or returns another value) the old row counts on its own.
`verify` accepts this link and still refuses any other link that hides a record with a Google
id. The sleeps held only in versions are not copied into new rows: Google still returns them
(the next `healthos reconcile --days N` stores each as its own row, with its response), and
a derived copy would be a second row of the same record.

**Dating.** Sleep and sleep summaries (`WAKE_DATED_TYPES`, `app/google_health/data_types.py`)
belong to the local day the sleep ended (the wake day): coverage (`retrieval_coverage`) and
Umay's physiology reader count a summary there, wherever the sleep began. Two sleeps that end
on one day are one value day and two records.

### Devices

`GET /api/devices` (System › **Cihazlarım**; `app/services/dashboard_views/devices.py`) lists every source the
archive has seen as one entry per **device**. A device is the set of source fingerprints joined
by live chain links (`raw_source_chains`, as the readers use them), across every data type:
"Google Fitbit Air" and "Fitbit Air" are one watch, shown under its newest name with the older
names in `previous_names`. A link the owner rejected, or one refuted by evidence, keeps the two
forms as two devices. The read is otherwise built from `raw_source_sightings` only: nothing is
written, there is no migration and no new table.

Per device: `name`, `platform`, `form_factor`, `manufacturer`, `application` (the package without
Google's per-install suffix), `recording_method`, `data_types` and `data_type_count`,
`first_seen`/`last_seen` (when the mirror first and last saw any of its forms), `last_data` (the
physical time of its newest record over every data type, `newest_record_at`; a re-fetch of old
records never makes a retired device current) and `status`:

| `status` | Meaning (defaults; `DEVICES_CONNECTED_HOURS`, `DEVICES_DELAYED_DAYS`) |
|---|---|
| `bagli` | newest record at most 48 hours old |
| `gecikmeli` | older than that, at most 7 days |
| `kopuk` | older than 7 days, or no dated record at all |

Every timestamp is UTC ISO 8601; the interface shows them in the chosen time zone with a relative
age ("2 saat önce"). The status describes the archive, not the watch's Bluetooth link: a device
whose records Google has not delivered yet reads `gecikmeli` even while it is worn.

When Google answers, the paired devices (`GET /google-health/devices`) are matched to the archive
devices by name (brand prefix dropped, one name containing the other); a matched device gains
`google_last_sync` and `google_device_type`, the rest are returned in `google_unmatched`, and
`google` is `ok`. When Google fails (no token, HTTP error) `google` is `unavailable` and the
archive's devices are returned all the same; the endpoint never answers 503 for a Google failure.
The response is sent with `Cache-Control: no-store` and requires the UI token like every other
route.

Umay's `health_archive` tool has the same read as the action `devices` (no date range; the
archive only, `google: not_queried`).

The Garmin source is one more entry (`id: garmin`, `provider: GARMIN`, named by
`GARMIN_DEVICE_NAME`) when `garmin_raw_records` holds a record: its `data_types` are the pulled
kinds, `last_seen` the last successful pull (`garmin_last_pull`), `last_data` the newest
normalized Garmin row and `status` follows the same thresholds. Every entry carries `precedence`
([Source precedence](source-precedence.md#source-precedence)); the response says `garmin: ok|off`. Apple Health
sources imported directly are entries too, merged into a Google `HEALTH_KIT` device of the same
name ([Apple Health](apple.md#apple-health-github-version)); the response says `apple: ok|off`.

## Raw record versions

Google re-sends records with a different payload when it recomputes them (HRV, calories, heart-rate
zones and resting heart rate often change for a day or two). A record is identified by
`(data_type, google_data_point_id)`; when a fetch brings a payload whose `payload_hash` differs
from the stored one, the old payload is **kept as a version**, never overwritten:

- `raw_health_data` holds the current version of every record; every reader (dashboards, Umay,
  normalized tables, `analytics_*` views) keeps using it. `payload_received_at` and
  `payload_source` (`sync`, `webhook`, `reconciliation`, `backfill`) say when and how the current
  payload arrived; both are NULL for payloads stored before migration `20261020_0011`.
- `raw_health_data_versions` holds every earlier payload, numbered per record (1 = oldest), with
  `received_at` (NULL if unknown), `last_seen_at` (the last fetch that still returned it),
  `superseded_at` (the fetch that replaced it), `source` (as above, `gap-repair` for
  `scripts/audit_date_gaps.py --repair`, `legacy` for pre-0011 payloads, `archive-import` for
  payloads recovered from old dumps) and `raw_id` (the current row). A database trigger writes the
  row in the same statement that replaces the payload, whenever `payload_hash` **or** the stored
  text (`raw_json::text`) changes, so a crash, two concurrent writers or a hand-written `UPDATE`
  cannot lose it. Re-receiving the same payload writes nothing. The table is append-only: triggers
  refuse `UPDATE`, `DELETE` and `TRUNCATE`; the one exception is renumbering `version`, which
  PostgreSQL allows only inside a transaction that set `healthos.renumber = '1'` (only
  `healthos import-versions` does). `raw_json` is compared as text, so even `1.0` for `1`
  counts as a change.
- **No record is ever deleted.** Triggers on `raw_health_data` refuse `DELETE` (with or without
  history), `TRUNCATE` (also when reached through `TRUNCATE ... CASCADE` from a referencing table)
  and any change of a record's identity (`id`, `data_type`, `google_data_point_id`,
  `first_seen_at`, `created_at`). Every protective trigger is `ENABLE ALWAYS`, so
  `session_replication_role = replica` does not bypass it either, and `healthos verify`
  fails if one is missing, disabled, redefined (other events, timing, level, columns or `WHEN`,
  e.g. `WHEN (false)`), joined by another trigger on a guarded table, or its function body was
  replaced. What a trigger cannot stop is the table owner's or a superuser's DDL (`DROP TABLE`,
  `ALTER TABLE ... DISABLE TRIGGER`). Production's `health` role is a superuser, but the app connects as
  the non-superuser application role (`POSTGRES_APP_USER`), which can do neither; `verify` detects
  the latter, and backups (see below) cover the former (see
  [the runbook](../RUNBOOK.md#guards-and-the-database-superuser)).
- Writers lock rows in key order (each page's rows are sorted before the UPSERT), so two
  overlapping pages written at the same time wait for each other instead of deadlocking. Derived-day
  invalidation is deferred to commit and applied in key order (migration `20261108_0028`); a page
  transaction PostgreSQL still aborts for a conflict is retried, four attempts in total.
- `source_start_time`, `source_end_time` and `source_timezone` are derived from `raw_json` by the
  parser; they are copied into each version but a recomputation alone (same payload) does not
  create one.
- A record that appears more than once in one page with different payloads becomes one version
  per payload, in page order.
- Normalized tables stay "current state": they are derived from the current raw payload and can be
  rebuilt from `raw_health_data`; the earlier payloads are only in the raw history.
- A record without history rows is version 1, so the migration copied nothing: the existing
  archive is not stored twice and the history grows only by payloads that really changed.

The view **`raw_health_data_history`** lists every version of every record: the history rows plus
the current row, with `version`, `is_current`, `valid_from` (when the version arrived; NULL if
unknown), `valid_to` (when it was replaced; NULL for the current version and for imported versions
whose replacement time is unknown), `last_seen_at`, `source`, `payload_hash` and `raw_json`. Filter
it by `data_type` and `google_data_point_id` (both indexed); for "what changed recently" query
`raw_health_data_versions` directly.

```sql
-- All versions of one record, oldest first
SELECT version, is_current, valid_from, valid_to, source, payload_hash, raw_json
FROM raw_health_data_history
WHERE data_type = 'heart-rate-variability'
  AND google_data_point_id = 'users/me/dataTypes/heart-rate-variability/dataPoints/…'
ORDER BY version;

-- Records replaced in the last 7 days
SELECT v.data_type, v.google_data_point_id, v.version AS replaced_version,
       v.superseded_at, v.payload_hash AS old_hash, r.payload_hash AS current_hash
FROM raw_health_data_versions v
JOIN raw_health_data r ON r.id = v.raw_id
WHERE v.superseded_at >= now() - interval '7 days'
ORDER BY v.superseded_at DESC;

-- Changes per data type
SELECT data_type, count(*) AS earlier_versions, count(DISTINCT raw_id) AS changed_records,
       max(superseded_at) AS last_change
FROM raw_health_data_versions
GROUP BY data_type
ORDER BY earlier_versions DESC;
```

The same from the command line (JSON, oldest version first; no secrets, payloads are included
unless `--no-payload`):

```sh
healthos history --data-type heart-rate-variability                 # 20 most recently changed records
healthos history --data-type heart-rate-variability --since 2026-10-13 --limit 100
healthos history --data-type resting-heart-rate --id 'users/me/dataTypes/…' --no-payload
```

### Recovering versions from old dumps

Before migration `20261020_0011` a changed payload overwrote the stored one; those versions only
survive in older backups. `healthos import-versions --from-db <database>` reads a restored
dump (any revision, including `json`-typed `raw_json` from before `20261020_0010`) and merges each
record's dump rows into its timeline by `last_seen_at`:

- A dump payload is *already present* only when it lands next to the same payload in that
  timeline. A payload that left and came back (A → B → A) therefore stays three versions; it is
  never deduplicated against an older, non-adjacent version.
- New payloads become versions with `source = 'archive-import'` and `superseded_at` set (the
  dump's own value when it has one, otherwise the payload's last sighting), so
  `history --since` lists them. Existing history rows keep their order and are only renumbered.
- A record the live archive does not have at all is **imported as its own row** (`payload_source =
  'archive-import'`, `source_deleted_at` set to the dump's value or the import time, so dashboards
  skip it), its latest dump payload as current version and earlier ones as history. Nothing that
  ever existed is lost. Live current rows are never changed; an import-restored row may take a
  newer payload from a later dump (the trigger keeps the old one).
- Rows whose stored hash does not match their payload (`source_hash_mismatches`) and payloads
  jsonb cannot store unchanged (`unrepresentable`: a float written as `1e+16`, `-0.0`, a `\u0000`
  escape) are skipped and listed, without aborting the rest; `--dry-run` reports both too, and the
  command exits 1 so the dump is kept.

Running it again adds nothing. Dumps are lossless when imported **newest first** (an older
sighting then always lands at the old end of a timeline); `--from-db` may be repeated and the
command sorts the dumps that way itself. `--from-db` takes a database name on the live server or a
full SQLAlchemy URL. Procedure: [RUNBOOK](../RUNBOOK.md#import-payload-versions-from-archive-dumps).
