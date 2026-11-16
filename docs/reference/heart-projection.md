# Google heart chart projection

`google_heart_hours` is disposable data, keyed by UTC hour. Each packet keeps every
validated original sample: raw row id, raw source time, parsed sample time, original
binary float (hexadecimal, including signed zero), and the complete platform value.
`current_heart_rate` is the only validator. Retained normalized BPM and timestamps
never supply a packet's measurement or bounds. The source/archive tables are unchanged.

The reader uses packets only after explicit initial maintenance has completed. A
method, revision or SHA-256 mismatch, corrupt packet, or missing hour uses the raw
reader for that hour. Source policy, sport windows, minute selection and finite means
remain read-time operations. SQL predicates other than the dashboard's marked default
(including source-deleted inclusion) retain the complete raw path.

## Maintenance

Until this fill completes, the dashboard reads raw data exactly as before (correct, and as
slow as before), while every write already pays for the invalidation triggers. Nothing starts
it on its own. During an authorized maintenance window, on the Compose host (the runtime image
has no `uv`; outside Compose use `python -m app.cli.heart_backfill` in the project venv):

```sh
docker compose exec -T app python -m app.cli.heart_backfill
```

Run it outside the nightly jobs (01:45–04:15 UTC) and never during a backup. The first
invocation captures the raw time horizon under the writer fence. The fill then reads the rest
of the horizon in one ordered pass from one `REPEATABLE READ` snapshot: sequential scans of the
raw, normalized and supersession tables, the dashboard eligibility applied as hash joins on
narrow rows, and one sort by raw source time that carries each payload once, so packets are
cut hour by hour in bounded memory. Its transaction forbids nested-loop, merge-join and plain
index-scan plans (per-row index probes are random reads on a rotating disk) and parallel
workers, and gives each hash or sort 64 MB. Vacuum cannot remove rows newer than that snapshot
while it runs. Hash joins and the sort use PostgreSQL temporary files; the command refuses to start
when the file system has less free space than about 1.4 times the raw table (in Compose its
own file system is the Docker disk that also holds the database volume; elsewhere check the
database's disk yourself).

Packets and the cursor commit about once per day of data, each in a short transaction that
tries the writer fence and polls a held one for up to two minutes. Each packet records the hour
revision seen in the reader's snapshot: a write that commits during the fill bumps its hours,
so their packets stay dirty, read raw data and are rebuilt by repair. Every 30 seconds it
prints hours read, packets written and elapsed seconds (counts only). Ctrl-C or `SIGTERM` stops
it after the current hour (or cancels the open transaction after 15 seconds); exit status 130.
Running it again resumes at the committed cursor, including a cursor left by the earlier
per-hour builder. `--hours N` bounds a run to N populated hours, but every run scans the whole
archive once, so a bounded loop is slower in total. Empty hours get no packet (their reader
reads the empty raw hour). It prints `İlk dolum tamamlandı.` once `ready` is committed; no
packet is used before that. A repeat on a completed horizon does nothing. No initial fill runs
in an HTTP request or the scheduler. Empty archives complete immediately. This command uses the
existing `DATABASE_URL`; it adds no settings, dependencies or network service.

The existing derived-refresh scheduler repairs at most sixteen dirty hours per run,
newest first (the dashboard reads recent hours), each in its own transaction. Raw/normalized corrections, quarantine, deletion/revival,
supersession and source-device changes dirty old/new affected hours in the writer's
transaction, including bulk SQL and history promotion/restore. Unchanged refetches
leave packets current. Hour revisions and changes roll back together.

Before-row PostgreSQL advisory fences serialize relevant heart writes and builds.
After-row triggers record each affected UTC hour once per transaction in
`google_heart_pending`; only that first touch bumps the hour's revision, so the dirty
hour is visible inside the writing transaction and rolls back with it (also with a
savepoint), and a page bumps each hour once. A deferred constraint trigger deletes the
transaction's pending rows at commit; no other transaction ever sees one. A builder tries the
fence once, never queues behind another builder/writer, and operates only at READ
COMMITTED. This deliberately conservative global heart fence also serializes writes
to different heart hours; a build holds it for one hour only. Other metric writes do
not acquire it. Non-default transaction isolation never builds packets.

The ingestion path writes one statement per record. Every trigger therefore filters with
a short `WHEN` (heart data type, or a non-Garmin normalized row): another data type calls
no trigger function, and PostgreSQL only evaluates those `WHEN` clauses, a few
microseconds per record. An UPDATE's value-change test runs inside the heart functions,
because a long `WHEN` is compiled for every statement. On a local 2.7-million-row
synthetic archive (PostgreSQL 16, medians of three) a 7,084-record heart batch took
0.65 s instead of 0.60 s without these triggers, a correction batch 1.01 s instead of
0.82 s, a 7,084-record steps batch 0.63 s instead of 0.60 s, and 100,000 heart records
in pages of 1,000 took 9.9 s instead of 9.2 s.

Migration 0036 adds only the three derived tables (packets, fill cursor, per-transaction
pending hours) and their trigger functions. It takes
`SHARE ROW EXCLUSIVE` on `raw_health_data`, `heart_rate`, `raw_health_data_supersessions`
and `raw_source_devices` (writers wait, readers and `pg_dump` do not), and waits at most
5 s for them (`lock_timeout`), then fails with nothing changed; run it again once the open
sync has committed. Its duration does not depend on the archive size. Its
downgrade removes these reproducible tables and their own triggers, preserving every
raw, normalized and history record. A method change requires maintenance/repair;
unmatched packets never silently omit measurements.

## Performance gate

Local PostgreSQL measurements do not establish the production HDD target. Publishing
that claim requires the separately authorized isolated PostgreSQL 17 / two-core /
2.8 GB / HDD experiment, including real cold-cache disclosure and twenty repetitions.
Until that experiment has run, the production performance target remains unverified. No query
timeout is introduced here.

Local synthetic validation on PostgreSQL 16.2 used 37,000 samples per UTC day and
30/365-day archives (1,110,000/13,505,000 raw and normalized rows). All original samples
were retained. On the 365-day archive the actual `days=7` dashboard took 0.734 s from
raw data and 0.283 s with packets after a PostgreSQL restart; direct seven-day scans
consumed 259,000 samples in 6.465/0.712 s. Three 7,084-record correction batches each
refreshed only five hours, in 0.281–0.296 s, without changing raw or normalized row counts;
normal history grew by the expected 21,252 versions.

These are local macOS measurements with 18 logical CPUs, 64 GiB RAM, 128 MiB PostgreSQL
buffers, `fsync=off` in temporary databases and concurrent tests. Only PostgreSQL's
buffers were cold; OS/disk caches were not cleared. The local packet table used
836,321,280 bytes for all 13,505,000 samples before correction batches. This neither
estimates production capacity nor establishes the separate production HDD gate.
