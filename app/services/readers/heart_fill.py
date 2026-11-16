"""The initial heart packet fill as one ordered pass instead of per-hour index probes.

Per hour, the dashboard eligibility predicate probes four indexes for every raw row
(normalized row, raw quarantine, deletion, supersession holder). On a rotating disk whose
cache cannot hold the archive, that is several random reads per sample. This pass reads the
tables sequentially, applies the same eligibility as set operations (hash joins on narrow
rows), and streams rows in raw source time order, so packets are cut hour by hour in
bounded memory.
"""

import json
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Select, Text, and_, cast, literal, null, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.dialect import database, dialect_insert
from app.models.health import HeartRate
from app.models.heart_projection import HeartHour
from app.models.identity_queries import in_force
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData, RawJSON

from . import current_heart, heart_projection

HEART = "heart-rate"
# The cost model assumes cached index probes; on the production disk each probe of a
# multi-gigabyte index is a random read, so only this read-only transaction forbids them.
# Without parallel workers (a fetched cursor runs none anyway) 64 MB bounds each hash or sort;
# with 4 MB the payload sort merges in several passes, rewriting its temporary files.
PLANNER = (
    "SET LOCAL enable_nestloop = off",
    "SET LOCAL enable_mergejoin = off",
    "SET LOCAL enable_indexscan = off",
    "SET LOCAL max_parallel_workers_per_gather = 0",
    "SET LOCAL work_mem = '64MB'",
)


@dataclass
class FillProgress:
    """Numbers only (no values): hours read, packets committed, elapsed seconds."""

    hours: int = 0
    packets: int = 0
    started: float = field(default_factory=time.monotonic)

    def line(self) -> str:
        elapsed = int(time.monotonic() - self.started)
        return f"İşlenen saat: {self.hours}, yazılan paket: {self.packets}, süre: {elapsed} sn"


def sample(raw_id: object, source_time: datetime, record: dict) -> list | None:
    """One packet entry, validated by ``current_heart_rate`` only (shared with repair)."""
    measurement = current_heart.current_heart_rate(record)
    if measurement is None:
        return None
    moment, bpm = measurement
    source = record.get("dataSource")
    platform = source.get("platform") if isinstance(source, dict) else None
    # Hex preserves the original binary float, including signed zero, without
    # passing through Numeric(10,3) or JSONB's numeric canonicalization.
    return [str(raw_id), source_time.isoformat(), moment.isoformat(), bpm.hex(), platform]


def rows(start: datetime, stop: datetime) -> Select:
    """Every heart raw row in ``[start, stop)``, each preceded by a marker when it counts.

    Ordered by (raw source time, id, tag): a narrow row (tag 0, no payload) for each counting
    raw row comes right before that row's payload row (tag 1). Only the narrow side joins,
    so hash joins never spill payloads, and the one sort carries each payload once.

    Counting equals ``DashboardSources().normalized(HeartRate, "heart-rate")`` joined on the
    raw key: (data_type, google_data_point_id) is unique, so its raw ``EXISTS`` clauses test
    this very row, and ``key_held_elsewhere`` becomes an anti-join on the held keys. Rows that
    do not count still arrive: an hour holding only those gets an empty packet, as the
    per-hour builder gives it.
    """
    raw, holder, wide = aliased(RawHealthData), aliased(RawHealthData), aliased(RawHealthData)
    link, normalized = aliased(RawHealthDataSupersession), aliased(HeartRate)
    held = (
        select(link.old_google_data_point_id.label("key"))
        .join(holder, holder.id == link.new_raw_id)
        .where(
            link.data_type == HEART,
            holder.source_deleted_at.is_(None),
            holder.is_quarantined.is_(False),
            in_force(link),
        )
        .distinct()
        .subquery()
    )
    counting = (
        select(raw.id, raw.source_start_time, literal(0).label("tag"), null().label("payload"))
        .join(
            normalized,
            and_(
                normalized.google_data_point_id == raw.google_data_point_id,
                normalized.is_quarantined.is_(False),
            ),
        )
        .outerjoin(held, held.c.key == raw.google_data_point_id)
        .where(
            raw.data_type == HEART,
            raw.source_start_time >= start,
            raw.source_start_time < stop,
            raw.is_quarantined.is_(False),
            raw.source_deleted_at.is_(None),
            held.c.key.is_(None),
        )
    )
    # Text: only counting payloads are decoded (``decode``), with the column type's own reader.
    payloads = select(wide.id, wide.source_start_time, literal(1), cast(wide.raw_json, Text)).where(
        wide.data_type == HEART, wide.source_start_time >= start, wide.source_start_time < stop
    )
    both = union_all(counting, payloads).subquery()
    return select(both).order_by(both.c.source_start_time, both.c.id, both.c.tag)


def decoder(session: AsyncSession) -> Callable[[str], Any]:
    """The JSON reader the ORM applies to ``raw_json`` on this connection's dialect.

    asyncpg decodes jsonb with the dialect's deserializer (default ``json.loads``) from the
    same text that ``jsonb::text`` returns; other dialects decode in a result processor.
    """
    dialect = session.get_bind().dialect
    process = RawJSON.dialect_impl(dialect).result_processor(dialect, None)
    return process or getattr(dialect, "_json_deserializer", None) or json.loads


async def revisions(session: AsyncSession, start: datetime, stop: datetime) -> dict:
    """Hour revisions in the reader's snapshot, the same one its samples come from."""
    found = await session.execute(
        select(HeartHour.bucket, HeartHour.revision).where(
            HeartHour.bucket >= start, HeartHour.bucket < stop
        )
    )
    return dict(found.tuples().all())


async def hours(
    session: AsyncSession, start: datetime, stop: datetime
) -> AsyncGenerator[tuple[datetime, list]]:
    """Yield (UTC hour, packet samples) for every hour holding a heart raw row, in order."""
    for statement in PLANNER:
        await database(session).control(session, text(statement))
    decode = decoder(session)
    stream = await session.stream(rows(start, stop).execution_options(yield_per=1000))
    bucket, packed, counted = None, [], None
    try:
        async for raw_id, source_time, tag, payload in stream:
            if not tag:
                counted = raw_id
                continue
            moment = heart_projection.hour(source_time)
            if moment != bucket:
                if bucket is not None:
                    yield bucket, packed
                bucket, packed = moment, []
            if counted == raw_id and (entry := sample(raw_id, source_time, decode(payload))):
                packed.append(entry)
        if bucket is not None:
            yield bucket, packed
    finally:
        await stream.close()


async def write(session: AsyncSession, bucket: datetime, packed: list, seen: int | None) -> None:
    """Store a packet built from a snapshot in which the hour had revision ``seen``.

    A writer that committed after that snapshot has bumped the hour, so the packet stays
    dirty (built != revision) and repair rebuilds it: no correction can be lost. An hour
    without a row in the snapshot that has one now was touched since: built 0 (dirty).
    """
    values = {
        "method": heart_projection.METHOD,
        "samples": packed,
        "digest": heart_projection.digest(packed),
    }
    insert = dialect_insert(session, HeartHour).values(
        bucket=bucket,
        revision=1 if seen is None else seen + 1,
        built_revision=1 if seen is None else seen,
        **values,
    )
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=[HeartHour.bucket],
            set_={**values, "built_revision": 0 if seen is None else seen},
        )
    )
