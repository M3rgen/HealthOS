"""Version 2 corrects civil-day offsets without changing version 1 normalized rows."""

from typing import cast

from sqlalchemy import ColumnElement, Table, and_, exists, or_, select
from sqlalchemy.orm import DeclarativeBase

from app.core.fit import FIT_EXERCISE, FIT_HEART_RATE, FIT_RECORD_PREFIX
from app.models.eligibility import normalized_counted

from .garmin_activity_state import activity_result_current
from .garmin_daily_state import DAILY_TABLES, daily_projection_current
from .garmin_membership import heart_set_current

VERSION = "v2"


def current_row(
    model: type[DeclarativeBase], *, activity_current: ColumnElement[bool] | None = None
):
    """Keep a legacy row readable until its v2 counterpart exists, then select only v2."""
    table = cast(Table, model.__table__)
    newer = table.alias("garmin_v2")
    version_current = ~exists(
        select(1).where(
            newer.c.provider == table.c.provider,
            newer.c.google_data_point_id == table.c.google_data_point_id + f":{VERSION}",
        )
    ).correlate(table)
    if table.name in DAILY_TABLES:
        version_current = and_(version_current, daily_projection_current(table))
    if table.name == "heart_rate":
        version_current = and_(version_current, heart_set_current(table))
    if table.name == "exercise_sessions":
        version_current = and_(
            version_current,
            activity_result_current(table) if activity_current is None else activity_current,
        )

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
