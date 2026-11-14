"""One-pass identity maps for workout publication and pre-result raw inputs.

Expanding aliases before the join lets PostgreSQL hash exact equality keys instead of
scanning every result's JSON identity for every normalized workout. No body crosses SQL.
"""

from sqlalchemy import and_, case, func, or_, select, union_all

from app.models.garmin import GarminActivityResult, GarminRawRecord

from .garmin_activity_identity import activity_identifier_sql
from .garmin_current import current_activity_ids


def completed_workout_results():
    """Maximum eligible result id per source alias, including invalid completions.

    A historical truncated key can only name the full identity in its linked body;
    it must never capture a separate short activity equal to that prefix.
    """
    state, raw = GarminActivityResult, GarminRawRecord
    identifier = activity_identifier_sql(raw.body)
    untruncated = or_(
        func.length(identifier) <= 64,
        state.source_record_id != "activities:" + func.substr(identifier, 1, 64),
    )
    eligible = (
        select(
            state.id.label("id"),
            state.source_record_id.label("source"),
            ("activities:" + identifier).label("full_source"),
            untruncated.label("untruncated"),
        )
        .join(raw, raw.id == state.raw_record_id)
        .where(
            raw.kind == "activities",
            or_(
                state.normalized_key.is_(None),
                and_(
                    untruncated,
                    or_(
                        state.normalized_key == "garmin:" + state.source_record_id,
                        state.normalized_key == "garmin:" + state.source_record_id + ":v2",
                    ),
                ),
                state.normalized_key == "garmin:activities:" + identifier,
                state.normalized_key == "garmin:activities:" + identifier + ":v2",
            ),
        )
        .cte("workout_results", nesting=True)
    )
    aliases = union_all(
        select(eligible.c.id, eligible.c.source).where(eligible.c.untruncated),
        select(eligible.c.id, eligible.c.full_source),
    ).subquery()
    return (
        select(aliases.c.source, func.max(aliases.c.id).label("id"))
        .group_by(aliases.c.source)
        .subquery("completed_workouts")
    )


def legacy_workout_inputs():
    """Current raw ids under the exact source and normalized aliases accepted before results.

    Counts preserve the old scalar lookup's behavior on inconsistent historical links:
    only an ambiguous alias needs the original scalar check. Ordinary reads use two
    equality joins to this map, so a pre-result archive is also read in one pass.
    """
    raw = GarminRawRecord
    identifier = activity_identifier_sql(raw.body)
    untruncated = or_(func.length(identifier) <= 64, raw.key != func.substr(identifier, 1, 64))
    inputs = (
        select(
            raw.id.label("id"),
            case((untruncated, "activities:" + raw.key)).label("source"),
            ("garmin:activities:" + identifier).label("normalized"),
        )
        .where(raw.id.in_(current_activity_ids()))
        .cte("workout_legacy_inputs", nesting=True)
    )
    aliases = union_all(
        select(inputs.c.id, inputs.c.source.label("key")),
        select(inputs.c.id, inputs.c.normalized),
        select(inputs.c.id, inputs.c.normalized + ":v2"),
    ).subquery()
    return (
        select(
            aliases.c.key,
            func.max(aliases.c.id).label("id"),
            func.count(func.distinct(aliases.c.id)).label("matches"),
        )
        .where(aliases.c.key.is_not(None))
        .group_by(aliases.c.key)
        .subquery("workout_input_aliases")
    )
