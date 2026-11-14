"""Workout measurements checked against their published input before SQL rounding."""

import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal, DecimalException

from sqlalchemy import String, Subquery, and_, case, func, or_, select, true
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.normalization import prepare_numeric
from app.core.providers import GARMIN
from app.core.timeutil import as_utc
from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.models.health import ExerciseSession
from app.parsers.common import number
from app.services import shared

from .garmin_activity_identity import activity_identifier_sql
from .garmin_current import current_activity_ids
from .garmin_versions import current_row
from .garmin_workout_inputs import completed_workout_results, legacy_workout_inputs

# Garmin truncates explicit ends to whole seconds and a derived end rounds to microseconds,
# so a duration may exceed its interval by under one second (maintainer, 2026-11-13).
GARMIN_DURATION_TOLERANCE = Decimal(1)
# Other manufacturers' timestamp precision is unknown, so their FIT sessions, and any
# provider without a rule of its own, get a wider margin than Garmin (maintainer, 2026-11-13).
OTHER_DURATION_TOLERANCE = Decimal(3)
_TOLERANCE = {GARMIN: GARMIN_DURATION_TOLERANCE}


def _source_number(source: object) -> Decimal | None:
    """Decode only the selected JSON member, retaining signs and boundary precision."""
    return number(
        json.loads(source, parse_float=Decimal, parse_int=Decimal)
        if isinstance(source, str)
        else None
    )


def _measurement(value: object, source: object, provider: str) -> float | None:
    """Only finite negatives are rejected; signed zero and numeric bounds stay intact."""
    if provider == GARMIN:
        # Decode numeric JSON tokens as Decimal: the default JSON float decoder
        # can underflow a source negative before its sign reaches this check.
        for candidate in (value, _source_number(source)):
            parsed = number(candidate)
            if parsed is not None and parsed.is_finite() and parsed < 0:
                return None
    return shared.finite_math.finite_number(value)


def _stored_rounding(source: Decimal | None, value: Decimal) -> bool:
    """Whether the stored value is exactly the column rounding of its published source."""
    fitted = {"duration_seconds": source}
    try:
        prepare_numeric(ExerciseSession, fitted)
    except DecimalException:
        return False
    return source is not None and fitted["duration_seconds"] == value


def _duration(row: ExerciseSession, source: object, provider: str) -> float | None:
    """Project an optional stored duration against its session's physical interval.

    Missing and SQL-overflowed optional values retain the interval fallback.
    A duration is valid when ``0 <= duration < interval + tolerance`` (``_TOLERANCE``).
    For Garmin's storable values, check the published input before rounding can
    disguise an out-of-range value; a stored value that only rounds its source is
    judged by that source. Normalized-only and FIT rows use their stored Decimal;
    neither an older result nor interval fallback can rescue an invalid measure.
    Endpoint derivation and eligibility stay with normalization.
    """
    delta = as_utc(row.end_time) - as_utc(row.start_time)
    interval = Decimal((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    interval = interval.scaleb(-6)
    value = row.duration_seconds
    if value is None:
        return float(interval)
    candidates: tuple[Decimal | None, ...] = (value,)
    if provider == GARMIN:
        published = _source_number(source)
        candidates = (None if _stored_rounding(published, value) else value, published)
    bound = interval + _TOLERANCE.get(provider, OTHER_DURATION_TOLERANCE)
    for candidate in candidates:
        if candidate is not None and candidate.is_finite() and not 0 <= candidate < bound:
            return None
    return shared.finite_math.finite_number(value)


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


def _workout_inputs(start: datetime, stop: datetime, provider: str) -> Subquery:
    """The provider's current workouts in the window, each with its raw input and publication.

    A completed result owns its immutable raw input. Only archives without results
    use the legacy observation fallback.
    """
    row, state = ExerciseSession, GarminActivityResult
    completed = completed_workout_results()
    aliases = legacy_workout_inputs()
    source, normalized = aliases.alias("workout_source"), aliases.alias("workout_normalized")
    ambiguous = or_(
        source.c.matches > 1,
        normalized.c.matches > 1,
        and_(
            source.c.id.is_not(None), normalized.c.id.is_not(None), source.c.id != normalized.c.id
        ),
    )
    legacy_id = case(
        (ambiguous, _legacy_input()), else_=func.coalesce(source.c.id, normalized.c.id)
    )
    input_id = case((state.id.is_not(None), state.raw_record_id), else_=legacy_id)
    published = or_(
        row.provider != GARMIN,
        state.id.is_(None),
        state.normalized_key == row.google_data_point_id,
    )
    # Resolve input aliases before filtering publication. PostgreSQL estimates that
    # predicate very low; pushing it into these joins makes it rescan each identity
    # map per workout. OFFSET 0 is a subquery boundary, with no row or order limit.
    return (
        select(row, input_id.label("input_id"), published.label("published"))
        .select_from(row)
        .outerjoin(completed, completed.c.source == row.source_record_id)
        .outerjoin(state, state.id == completed.c.id)
        .outerjoin(
            source,
            and_(source.c.key == row.source_record_id, source.c.key.startswith("activities:")),
        )
        .outerjoin(
            normalized,
            and_(
                normalized.c.key == row.google_data_point_id,
                normalized.c.key.startswith("garmin:activities:"),
            ),
        )
        .where(
            row.provider == provider,
            current_row(row, activity_current=true()),
            row.is_quarantined.is_(False),
            row.start_time >= start,
            row.start_time < stop,
        )
        .offset(0)
        .subquery("workout_inputs")
    )


async def workout_values(
    session: AsyncSession, start: datetime, stop: datetime, provider: str
) -> AsyncIterator[tuple[ExerciseSession, float | None, float | None, float | None]]:
    """Read just three raw members in one bounded query, without observing pending inputs.

    Normalized-only and FIT rows check their stored values. Other providers keep their
    calorie and distance conversion policy.
    """
    raw = GarminRawRecord
    inputs = _workout_inputs(start, stop, provider)
    exercise = aliased(ExerciseSession, inputs)
    query = (
        select(
            exercise,
            sql_cast(raw.body.op("->")("calories"), String).label("source_calories"),
            sql_cast(raw.body.op("->")("distance"), String).label("source_distance"),
            sql_cast(raw.body.op("->")("duration"), String).label("source_duration"),
        )
        .outerjoin(raw, (exercise.provider == GARMIN) & (raw.id == inputs.c.input_id))
        .where(inputs.c.published)
        .order_by(exercise.start_time)
    )
    rows = await session.stream(query.execution_options(yield_per=1000))
    try:
        async for exercise, calories, distance, duration in rows.tuples():
            yield (
                exercise,
                _measurement(exercise.calories_kcal, calories, provider),
                _measurement(exercise.distance_meters, distance, provider),
                _duration(exercise, duration, provider),
            )
    finally:
        await rows.close()
