"""Append-only time choices and SQL invalidation against current, usable heart evidence."""

from datetime import date

from sqlalchemy import Select, case, exists, func, insert, literal_column, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.garmin import GarminDayTime, GarminRawRecord, GarminTimeEvidence


def time_choice_pending(current_heart_ids: Select):
    """Compare only the last choice, detecting first/closer evidence and A -> B -> A.

    One ordered pass replaces per-row nearest searches: running evidence counts group
    every last choice with its predecessor (same day included) and its successor, so
    the work grows with the archive, not with archive x heart days. Predecessor and
    successor keep the converter's earlier-day tie break. Only ids and ordinal
    metadata participate; archived bodies never enter this query.
    """
    raw, choices, evidence = GarminRawRecord, GarminDayTime, GarminTimeEvidence
    rows = union_all(
        select(
            raw.key.label("day"),
            literal_column("0").label("choice"),
            evidence.raw_record_id.label("record"),
            evidence.day_ordinal.label("ordinal"),
        )
        .join(evidence, evidence.raw_record_id == raw.id)
        .where(raw.id.in_(current_heart_ids), evidence.offset_minutes.is_not(None)),
        select(choices.day, literal_column("1"), choices.raw_record_id, choices.day_ordinal).where(
            choices.id.in_(select(func.max(choices.id)).group_by(choices.day))
        ),
    ).subquery()
    found = case((rows.c.choice == 0, 1), else_=0)
    order = {
        "before": (rows.c.day, rows.c.choice),
        "after": (rows.c.day.desc(), rows.c.choice.desc()),
    }
    ranked = select(
        rows,
        *(
            func.sum(found).over(order_by=by, rows=(None, 0)).label(side)
            for side, by in order.items()
        ),
    ).subquery()
    near = select(
        ranked.c.day,
        ranked.c.choice,
        ranked.c.record,
        ranked.c.ordinal,
        *(
            func.max(case((ranked.c.choice == 0, ranked.c[column])))
            .over(partition_by=ranked.c[side])
            .label(f"{side}_{column}")
            for side in order
            for column in ("record", "ordinal")
        ),
    ).subquery()
    left, right = near.c.before_record, near.c.after_record
    selected = case(
        (left.is_(None), right),
        (right.is_(None), left),
        (near.c.ordinal - near.c.before_ordinal <= near.c.after_ordinal - near.c.ordinal, left),
        else_=right,
    )
    stale = select(near.c.day).where(near.c.choice == 1, near.c.record.is_distinct_from(selected))
    return or_(~exists().where(choices.day == raw.key), raw.key.in_(stale.correlate(None)))


async def write_day_times(session: AsyncSession, choices: dict[str, int | None]) -> None:
    """Publish the actual selected raw id (or unknown) with the day's normalized values."""
    if not choices:
        return
    latest = (
        select(func.max(GarminDayTime.id))
        .where(GarminDayTime.day.in_(choices))
        .group_by(GarminDayTime.day)
    )
    previous = {
        row.day: row.raw_record_id
        for row in await session.scalars(select(GarminDayTime).where(GarminDayTime.id.in_(latest)))
    }
    changed = [
        {
            "day": day,
            "day_ordinal": date.fromisoformat(day).toordinal(),
            "raw_record_id": identifier,
        }
        for day, identifier in choices.items()
        if day not in previous or previous[day] != identifier
    ]
    if changed:
        await session.execute(insert(GarminDayTime), changed)
