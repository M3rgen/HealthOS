"""Append-only membership of daily Garmin heart samples, committed with normalization.

Only prepared rows belong to the set. A selected raw day producing no samples commits an
empty set; an absent raw day does nothing. Reads use the last committed set, never payloads
or the unnormalized latest observation. Old archives retain v1/v2 fallback until rebuilt.
"""

from typing import Any

from sqlalchemy import Table, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.normalization import payload_hash
from app.core.providers import GARMIN
from app.models.garmin import GarminHeartMember, GarminHeartSet, GarminRawRecord


def heart_set_pending(record: type[GarminRawRecord] = GarminRawRecord):
    """An empty completed set is evidence too; only its selected raw version matters."""
    completed = (
        select(GarminHeartSet.raw_record_id)
        .where(GarminHeartSet.source_record_id == "heart-rate:" + record.key)
        .order_by(GarminHeartSet.id.desc())
        .limit(1)
        .correlate(record)
        .scalar_subquery()
    )
    return (record.kind == "heart-rate") & completed.is_distinct_from(record.id)


def heart_set_current(table: Table):
    """Indexed set lookup, independent of raw-version selection and user quarantine."""
    sets = GarminHeartSet.__table__
    members = GarminHeartMember.__table__
    latest = (
        select(func.max(sets.c.id))
        .where(sets.c.source_record_id == table.c.source_record_id)
        .correlate(table)
        .scalar_subquery()
    )
    return or_(
        table.c.provider != GARMIN,
        table.c.source_record_id.is_(None),
        ~table.c.source_record_id.startswith("heart-rate:"),
        latest.is_(None),
        exists(
            select(1).where(
                members.c.set_id == latest, members.c.sample_key == table.c.google_data_point_id
            )
        ).correlate(table),
    )


async def write_heart_sets(
    session: AsyncSession,
    record_ids: dict[tuple[str, str], int],
    rows: list[dict[str, Any]],
) -> None:
    """Append changed sets and members in batches, in the caller's normalization transaction."""
    sources = {
        f"heart-rate:{key}": identifier
        for (kind, key), identifier in record_ids.items()
        if kind == "heart-rate"
    }
    if not sources:
        return
    keys: dict[str, set[str]] = {source: set() for source in sources}
    for row in rows:
        keys[row["source_record_id"]].add(row["google_data_point_id"])
    latest = (
        select(func.max(GarminHeartSet.id))
        .where(GarminHeartSet.source_record_id.in_(sources))
        .group_by(GarminHeartSet.source_record_id)
    )
    previous = {
        row.source_record_id: (row.raw_record_id, row.members_sha256)
        for row in await session.scalars(
            select(GarminHeartSet).where(GarminHeartSet.id.in_(latest))
        )
    }
    changed = []
    for source, identifier in sources.items():
        digest = payload_hash(sorted(keys[source]))
        if previous.get(source) != (identifier, digest):
            changed.append(
                {"source_record_id": source, "raw_record_id": identifier, "members_sha256": digest}
            )
    if not changed:
        return
    sets = await session.execute(
        insert(GarminHeartSet).returning(GarminHeartSet.id, GarminHeartSet.source_record_id),
        changed,
    )
    members = [
        {"set_id": identifier, "sample_key": key}
        for identifier, source in sets
        for key in sorted(keys[source])
    ]
    for offset in range(0, len(members), 1000):
        await session.execute(insert(GarminHeartMember), members[offset : offset + 1000])
