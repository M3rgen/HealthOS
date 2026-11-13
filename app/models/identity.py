"""Stable identities of raw records and the duplicates they replace (migration 20261021_0012).

``raw_health_data_identity`` maps a record's fingerprint-specific v2 key
(``RecordIdentity.specific_key``, app/parsers/identity.py) to the raw row that holds it, for
rows whose own id does not say so: a raw row's ``google_data_point_id`` can never change (the
frozen identity trigger of 0011), so rows stored under a v1 id are not re-keyed; the reconciler
(``healthos identity-backfill``) gives each of them an alias, and ``store_records`` finds
a returning record through it. Rows stored since 0012 need none: their id
(``synthetic:v2s:<hash>``) is computed from the key and the fingerprint.

``raw_health_data_supersessions`` links a duplicate row that was archived under an older id
of the same record to a row that holds the record now, with the two rows' source fingerprints
when they were linked. A superseded row is kept with its payload, versions,
``source_deleted_at`` and events (it is history). A link is *in force* while its two
fingerprints are one, or one device (``raw_source_devices``); a link whose fingerprints are
no longer one device (its chain was rejected or refuted, or the owner decided them distinct)
stays as history but no longer hides anything, and is in force again when the owner joins
them again. A row that an in-force link joins to a live row no longer counts: dashboards, Umay
and the ``analytics_*`` views skip it, as they skip a source deletion (``held_elsewhere``).
When every such holder is marked deleted at the source, a live superseded row counts again: a
link never hides a live record behind a deleted one. A row may be linked more than once (a
link no longer in force, then another holder); the newest in-force link names its holder.

``raw_source_chains`` records which source fingerprints are one device seen before and after
a change (a ``displayName`` rename, a dropped package suffix): one row per link ``from -> to``,
decided per data type and source family by the reconciler from the sources' sighting
timelines (app/services/source_chains.py), with the spans it was decided on as ``evidence``,
or by the owner (``healthos identity-decide link``). A link joins the two forms into one
device in both directions: the old form coming back later does not undo it.
``raw_source_chain_events`` records when a link was *refuted* (the two forms turned out to be
two devices). A link is *live* unless the owner's latest decision about it is ``reject`` or
the two forms are distinct (refuted, or decided so by the owner); only live links make a
device, and only live links must form chains (one successor and one predecessor each, no
cycle: checked when a link is recorded or revived, and by verify). A rejected or refuted link
leaves its fingerprints free for another link.

``raw_source_devices`` logs, per fingerprint that ever had a link, the device it belongs to
(its smallest fingerprint) whenever that changes; the latest row per fingerprint is current.
Readers decide whether a link is in force from it alone (computed once per query), and verify
checks it against the links and decisions.

``raw_source_pairs`` records evidence of two distinct sources: a key returned in one response
under two fingerprints with two different readings (one record has one value), with when its
fetch began. Two fingerprints are *established distinct* by evidence from two fetches of which
the later returned a record newer than the earlier fetch (both still produce new records): one
response alone, or the same old records returned again, never is (app/services/source_chains.py).

``raw_source_decisions`` records the owner's decisions about two fingerprints (link, reject or
confirm a chain; mark a pair distinct or not distinct). The latest decision per question wins.

``raw_source_agreements`` records, per pair of fingerprints with evidence of two devices and per
key both hold, whether some row of each holds the same reading (appended when that changes;
the latest row per key is current). Counting the latest rows of a pair gives how many of its
shared keys agree: counted per key, so a key counted again is never counted twice.

These eight tables are append-only (no ``UPDATE``, ``DELETE`` or ``TRUNCATE``; triggers
``ENABLE ALWAYS``). ``raw_source_sightings`` - when each source fingerprint was first and last
seen, by the database clock, and the newest record time it returned - may only widen: no
``DELETE`` or ``TRUNCATE``, and an ``UPDATE`` may only move ``first_seen_at`` earlier,
``last_seen_at`` and ``newest_record_at`` later and ``complete`` to true (its key, class, label
and ``dataSource`` never change). ``healthos verify`` checks all of these triggers like
the ones of 0011.

``identity_reconcile_state`` is bookkeeping: per data type, how far the reconciler got, the
agreement counts per pair (``raw_source_agreements`` summed; verify compares them) and any
re-settlement a run or decision still owes. ``identity_deferred_deletions`` is bookkeeping too:
one row per raw row whose deletion a complete fetch deferred, until a later complete fetch of
its window decides it (``resolved_at``).

The trigger SQL is duplicated, frozen, in the migration; tests/test_identity_keys.py compares
the two.


Implementations live in identity_constants, identity_tables, identity_queries and
identity_guards. Only the exports still consumed by the protected deletion lane remain here.
"""

from app.models.identity_queries import in_force as in_force
from app.models.identity_queries import plan_each_query as plan_each_query
from app.models.identity_tables import IdentityDeferredDeletion as IdentityDeferredDeletion
from app.models.identity_tables import RawHealthDataSupersession as RawHealthDataSupersession
