"""Append-only daily projection results, published with their normalized values.

An observed raw input can produce no prepared row. Record that outcome without changing
old measurements or user quarantine. Missing inputs alone never invalidate anything. The
converter still owns all precedence and fallback choices; this module records its result.
"""

from typing import Any

from sqlalchemy import BigInteger, Table, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.providers import GARMIN
from app.models.garmin import GarminDailyProjection, GarminRawRecord

from .garmin_time_state import write_day_times

# Projection source kind -> all raw kinds the existing converter may use for its value.
INPUTS = {
    "steps": ("steps",),
    "distance": ("steps",),
    "resting-heart-rate": ("daily-summaries", "heart-rate"),
    "sleep": ("sleep", "sleep-summaries"),
    "hrv": ("hrv-summaries", "hrv"),
    "spo2": ("spo2",),
    "stress": ("stress", "daily-summaries"),
    "body-battery": ("daily-summaries",),
}
DAILY_TABLES = {
    "steps",
    "distance",
    "resting_heart_rate",
    "sleep_sessions",
    "heart_rate_variability",
    "oxygen_saturation",
    "scalar_metrics",
}


def daily_projection_pending(record: type[GarminRawRecord] = GarminRawRecord):
    """Compare each current input with the last completed outcome, including NULL.

    All alternate inputs matter, even when the converter preferred another one.
    Looking at only the latest outcome also detects A -> B -> A observations;
    finding an older successful conversion of A would incorrectly leave B visible.
    Raw-only kinds have no projections and therefore cannot create pending work.
    """
    pending = []
    for projection, kinds in INPUTS.items():
        for kind in kinds:
            completed = (
                select(GarminDailyProjection.raw_record_ids[kind].as_string().cast(BigInteger))
                .where(GarminDailyProjection.source_record_id == projection + ":" + record.key)
                .order_by(GarminDailyProjection.id.desc())
                .limit(1)
                .correlate(record)
                .scalar_subquery()
            )
            pending.append((record.kind == kind) & completed.is_distinct_from(record.id))
    return or_(*pending)


def daily_projection_current(table: Table):
    """Use the last completed projection; keep legacy rows until evidence is built."""
    states = GarminDailyProjection.__table__
    latest = (
        select(func.max(states.c.id))
        .where(states.c.source_record_id == table.c.source_record_id)
        .correlate(table)
        .scalar_subquery()
    )
    return or_(
        table.c.provider != GARMIN,
        latest.is_(None),
        exists(
            select(1).where(
                states.c.id == latest,
                states.c.normalized_key == table.c.google_data_point_id,
            )
        ).correlate(table),
    )


def _outcomes(record_ids, rows):
    prepared = {
        row["source_record_id"]: row["google_data_point_id"]
        for model, values in rows.items()
        if model.__tablename__ in DAILY_TABLES
        for row in values
    }
    days: dict[str, dict[str, int]] = {}
    for (kind, day), identifier in record_ids.items():
        if kind != "activities":
            days.setdefault(day, {})[kind] = identifier
    for day, records in sorted(days.items()):
        for projection, kinds in INPUTS.items():
            inputs = {kind: records[kind] for kind in kinds if kind in records}
            if inputs:
                source = f"{projection}:{day}"
                yield {
                    "source_record_id": source,
                    "raw_record_ids": inputs,
                    "normalized_key": prepared.get(source),
                }


async def write_daily_projections(
    session: AsyncSession,
    record_ids: dict[tuple[str, str], int],
    rows: dict[type[DeclarativeBase], list[dict[str, Any]]],
    *,
    time_choices: dict[str, int | None] | None = None,
) -> None:
    """Append changed outcomes only, including absence after numeric preparation."""
    outcomes = list(_outcomes(record_ids, rows))
    for offset in range(0, len(outcomes), 1000):
        batch = outcomes[offset : offset + 1000]
        latest = (
            select(func.max(GarminDailyProjection.id))
            .where(
                GarminDailyProjection.source_record_id.in_([r["source_record_id"] for r in batch])
            )
            .group_by(GarminDailyProjection.source_record_id)
        )
        previous = {
            row.source_record_id: (row.raw_record_ids, row.normalized_key)
            for row in await session.scalars(
                select(GarminDailyProjection).where(GarminDailyProjection.id.in_(latest))
            )
        }
        changed = [
            row
            for row in batch
            if previous.get(row["source_record_id"])
            != (row["raw_record_ids"], row["normalized_key"])
        ]
        if changed:
            await session.execute(insert(GarminDailyProjection), changed)

    if time_choices is not None:
        await write_day_times(session, time_choices)
