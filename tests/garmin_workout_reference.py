"""Independent pre-733 workout SQL, including result selection and legacy eligibility.

Keep these predicates frozen: equivalence tests must not share the replacement selection.
Only unchanged JSON identity decoding and current-observation ranking are reused.
"""

from typing import cast

from sqlalchemy import String, Table, and_, case, exists, func, or_, select
from sqlalchemy import cast as sql_cast

from app.core.fit import FIT_EXERCISE, FIT_HEART_RATE, FIT_RECORD_PREFIX
from app.core.providers import GARMIN
from app.models.eligibility import normalized_counted
from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.models.health import ExerciseSession
from app.services.providers.garmin_activity_identity import activity_identifier_sql
from app.services.providers.garmin_current import current_activity_ids

VERSION = "v2"
DAILY_TABLES = ()


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


def current_row(model):
    """Keep a legacy row readable until its v2 counterpart exists, then select only v2."""
    table = cast(Table, model.__table__)
    newer = table.alias("garmin_v2")
    version_current = ~exists(
        select(1).where(
            newer.c.provider == table.c.provider,
            newer.c.google_data_point_id == table.c.google_data_point_id + f":{VERSION}",
        )
    ).correlate(table)
    if table.name == "exercise_sessions":
        version_current = and_(version_current, activity_result_current(table))

    fit_type = {"exercise_sessions": FIT_EXERCISE.name, "heart_rate": FIT_HEART_RATE.name}.get(
        table.name
    )
    if fit_type is None:
        return version_current
    # FIT uses the shared raw identity archive rather than Garmin API record versions.
    # Apply its live supersession/quarantine rules on every Garmin reader as well.
    return and_(
        version_current,
        or_(
            table.c.source_record_id.is_(None),
            ~table.c.source_record_id.startswith(FIT_RECORD_PREFIX),
            normalized_counted(model, (fit_type,)),
        ),
    )


def _legacy_input():
    """Pre-result archives use current full identities, never an ambiguous raw prefix."""
    raw, row = GarminRawRecord, ExerciseSession
    identifier = activity_identifier_sql(raw.body)
    untruncated = or_(
        func.length(identifier) <= 64,
        raw.key != func.substr(identifier, 1, 64),
    )
    return (
        select(raw.id)
        .where(
            raw.id.in_(current_activity_ids()),
            or_(
                (row.source_record_id == "activities:" + raw.key) & untruncated,
                row.google_data_point_id == "garmin:activities:" + identifier,
                row.google_data_point_id == "garmin:activities:" + identifier + ":v2",
            ),
        )
        .correlate(row)
        .scalar_subquery()
    )


def reference_query(start, stop, provider=GARMIN):
    row, state, raw = ExerciseSession, GarminActivityResult, GarminRawRecord
    input_id = case((state.id.is_not(None), state.raw_record_id), else_=_legacy_input())
    query = (
        select(
            row,
            sql_cast(raw.body.op("->")("calories"), String).label("source_calories"),
            sql_cast(raw.body.op("->")("distance"), String).label("source_distance"),
            sql_cast(raw.body.op("->")("duration"), String).label("source_duration"),
        )
        .select_from(row)
        .outerjoin(state, state.id == activity_result_id(cast(Table, row.__table__)))
        .outerjoin(raw, (row.provider == GARMIN) & (raw.id == input_id))
        .where(
            row.provider == provider,
            current_row(row),
            row.is_quarantined.is_(False),
            row.start_time >= start,
            row.start_time < stop,
        )
        .order_by(row.start_time)
    )
    return query
