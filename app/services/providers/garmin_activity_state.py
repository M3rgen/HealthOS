"""Append-only activity conversion results, visible with the normalized values.

Readers use the latest completed result, never an unnormalized raw observation. A valid
activity outside the requested window publishes nothing; missing records do nothing too.
Old normalized rows and user quarantine decisions are retained on invalid corrections.
"""

from typing import Any

from sqlalchemy import ColumnElement, Table, and_, exists, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import GARMIN
from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.models.health import ExerciseSession

from .garmin_activity_identity import activity_identifier, activity_identifier_sql, activity_key


def completed_activity_versions(identity: ColumnElement[str]):
    """Last committed conversion per full identity, including explicit invalid results.

    A result for an older version is not completion of the current version, even if
    that version was converted before a reversion. Join through immutable raw ids so
    legacy truncated source keys cannot merge different full activity identities.
    Only metadata crosses this query; completed bodies need not be loaded in Python.
    """
    raw, states = GarminRawRecord, GarminActivityResult
    identifier = activity_identifier_sql(raw.body)
    untruncated = or_(
        func.length(identifier) <= 64,
        states.source_record_id != "activities:" + func.substr(identifier, 1, 64),
    )
    ranked = (
        select(
            identity.label("identity"),
            states.raw_record_id.label("raw_id"),
            func.row_number()
            .over(partition_by=identity, order_by=states.id.desc())
            .label("position"),
        )
        .join(raw, raw.id == states.raw_record_id)
        .where(
            raw.kind == "activities",
            or_(
                states.normalized_key.is_(None),
                and_(
                    untruncated,
                    or_(
                        states.normalized_key == "garmin:" + states.source_record_id,
                        states.normalized_key == "garmin:" + states.source_record_id + ":v2",
                    ),
                ),
                states.normalized_key == "garmin:activities:" + identifier,
                states.normalized_key == "garmin:activities:" + identifier + ":v2",
            ),
        )
        .subquery()
    )
    return select(ranked.c.identity, ranked.c.raw_id).where(ranked.c.position == 1).subquery()


def activity_result_id(table: Table):
    """Latest completed result for this exact identity, including legacy aliases."""
    states = GarminActivityResult.__table__
    raw = GarminRawRecord.__table__
    identifier = activity_identifier_sql(raw.c.body)
    # Old results used a truncated source key even though their normalized key used
    # the full id. Only the linked body's full id can establish that legacy alias.
    # In particular, such a result must not hide a real short id equal to its prefix.
    untruncated_source = or_(
        func.length(identifier) <= 64,
        states.c.source_record_id != "activities:" + func.substr(identifier, 1, 64),
    )
    source_matches = or_(
        and_(
            states.c.source_record_id == table.c.source_record_id,
            untruncated_source,
        ),
        table.c.source_record_id == "activities:" + identifier,
    )
    return (
        select(func.max(states.c.id))
        .select_from(states.join(raw, raw.c.id == states.c.raw_record_id))
        .where(
            raw.c.kind == "activities",
            source_matches,
            or_(
                states.c.normalized_key.is_(None),
                and_(
                    untruncated_source,
                    or_(
                        states.c.normalized_key == "garmin:" + states.c.source_record_id,
                        states.c.normalized_key == "garmin:" + states.c.source_record_id + ":v2",
                    ),
                ),
                states.c.normalized_key == "garmin:activities:" + identifier,
                states.c.normalized_key == "garmin:activities:" + identifier + ":v2",
            ),
        )
        .correlate(table)
        .scalar_subquery()
    )


def activity_result_current(table: Table):
    """Keep v1/v2 fallback until a completed conversion result exists for this source."""
    states = GarminActivityResult.__table__
    latest = activity_result_id(table)
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


async def inherit_activity_quarantine(
    session: AsyncSession, rows: list[dict[str, Any]], bodies: dict[str, Any]
) -> None:
    """New bounded keys inherit exclusions only from exact full-id legacy rows.

    Prefix-only historical links are ambiguous and never used. write_rows ignores
    exclusion fields on existing rows, so later explicit user decisions stay intact.
    """
    aliases: dict[str, list[str]] = {}
    for body in bodies.values():
        identifier, key = activity_identifier(body), activity_key(body)
        if identifier is not None and len(identifier) > 64 and key is not None:
            aliases[f"activities:{key}"] = [
                f"garmin:activities:{identifier}:v2",
                f"garmin:activities:{identifier}",
            ]
    keys = [key for names in aliases.values() for key in names if len(key) <= 512]
    excluded = {}
    for offset in range(0, len(keys), 1000):
        for row in await session.scalars(
            select(ExerciseSession).where(
                ExerciseSession.provider == GARMIN,
                ExerciseSession.is_quarantined.is_(True),
                ExerciseSession.google_data_point_id.in_(keys[offset : offset + 1000]),
            )
        ):
            # Both fields were written from the full identifier; neither a prefix
            # nor a result's potentially truncated source id proves this identity.
            if row.google_data_point_id in (
                f"garmin:{row.source_record_id}",
                f"garmin:{row.source_record_id}:v2",
            ):
                excluded[row.google_data_point_id] = row.quarantine_reason
    for values in rows:
        for key in aliases.get(values["source_record_id"], []):
            if key in excluded:
                values.update(is_quarantined=True, quarantine_reason=excluded[key])
                break


async def write_activity_results(
    session: AsyncSession,
    record_ids: dict[tuple[str, str], int],
    results: dict[str, str | None],
) -> None:
    """Append changed outcomes only, in the caller's normalization transaction."""
    outcomes = [
        {
            "source_record_id": f"activities:{key}",
            "raw_record_id": record_ids["activities", key],
            "normalized_key": normalized_key,
        }
        for key, normalized_key in results.items()
    ]
    for offset in range(0, len(outcomes), 1000):
        batch = outcomes[offset : offset + 1000]
        latest = (
            select(func.max(GarminActivityResult.id))
            .where(
                GarminActivityResult.source_record_id.in_([r["source_record_id"] for r in batch])
            )
            .group_by(GarminActivityResult.source_record_id)
        )
        previous = {
            row.source_record_id: (row.raw_record_id, row.normalized_key)
            for row in await session.scalars(
                select(GarminActivityResult).where(GarminActivityResult.id.in_(latest))
            )
        }
        changed = [
            row
            for row in batch
            if previous.get(row["source_record_id"])
            != (row["raw_record_id"], row["normalized_key"])
        ]
        if changed:
            await session.execute(insert(GarminActivityResult), changed)
