"""Select current Garmin versions without loading old payloads or HTTP response bodies."""

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.garmin import GarminRawRecord, GarminRecordObservation

from .garmin_activity_identity import activity_identifier_sql, activity_key
from .garmin_activity_state import completed_activity_versions
from .garmin_daily_state import INPUTS, daily_projection_pending
from .garmin_membership import heart_set_pending
from .garmin_time_state import time_choice_pending


def activity_identity(record=GarminRawRecord):
    """Group legacy truncated and current keys by the body's complete activity id.

    SQL selects only the newest body, including when two old versions share a raw
    key but belong to different activities. Missing ids keep their archival key.
    """
    return func.coalesce(activity_identifier_sql(record.body), record.key)


def current_activity_ids(
    *criteria: ColumnElement[bool],
    seen_since: datetime | None = None,
    include_pending: bool = False,
):
    """Rank full identities across old and new keys without scanning bodies in Python.

    Rank real sightings before the no-observation fallback. Window ordering avoids
    a correlated full-archive JSON scan for each activity in a multi-year backfill.
    Periodic pages also include identities whose last completed conversion names a
    different raw version (or is absent), regardless of their observation date.
    """
    raw, observation = GarminRawRecord, GarminRecordObservation
    identity = activity_identity()
    selected = (
        select(
            identity.label("identity"),
            raw.id.label("raw_id"),
            observation.observed_at.label("observed_at"),
            func.max(raw.first_seen_at).over(partition_by=identity).label("legacy_seen_at"),
            func.row_number()
            .over(
                partition_by=identity,
                order_by=(
                    observation.observed_at.desc().nulls_last(),
                    observation.id.desc().nulls_last(),
                    raw.id.desc(),
                ),
            )
            .label("position"),
        )
        .outerjoin(observation, observation.raw_record_id == raw.id)
        .where(raw.kind == "activities", *criteria)
        .subquery()
    )
    query = select(selected.c.raw_id).where(selected.c.position == 1)
    if seen_since is not None:
        recent = func.coalesce(selected.c.observed_at, selected.c.legacy_seen_at) >= seen_since
        if include_pending:
            completed = completed_activity_versions(identity)
            query = query.outerjoin(completed, completed.c.identity == selected.c.identity)
            recent = or_(
                recent, completed.c.raw_id.is_(None), completed.c.raw_id != selected.c.raw_id
            )
        query = query.where(recent)
    return query


def current_record_ids(
    *criteria: ColumnElement[bool],
    seen_since: datetime | None = None,
    activities: bool = False,
    include_pending: bool = False,
):
    """One raw id per key: last observation (time, id), or legacy max raw id.

    The legacy fallback is per key, not per version: once a key has observation evidence,
    only that evidence chooses its current content. No historical sightings are inferred.
    ``seen_since`` selects keys seen in this run, including returns to an older raw version.
    Activity pages can additionally recover pending versions with ``include_pending``.
    """
    if activities:
        return current_activity_ids(
            *criteria, seen_since=seen_since, include_pending=include_pending
        )
    keys = (
        select(
            GarminRawRecord.kind,
            GarminRawRecord.key,
            func.max(GarminRawRecord.id).label("legacy_id"),
            func.max(GarminRawRecord.first_seen_at).label("legacy_seen_at"),
        )
        .where(*criteria)
        .group_by(GarminRawRecord.kind, GarminRawRecord.key)
        .subquery()
    )
    version = aliased(GarminRawRecord)
    observation = GarminRecordObservation
    latest = (
        select(observation.raw_record_id)
        .join(version, version.id == observation.raw_record_id)
        .where(version.kind == keys.c.kind, version.key == keys.c.key)
        .order_by(observation.observed_at.desc(), observation.id.desc())
        .limit(1)
        .correlate(keys)
    )
    query = select(func.coalesce(latest.scalar_subquery(), keys.c.legacy_id))
    if seen_since is not None:
        observed_at = latest.with_only_columns(observation.observed_at).scalar_subquery()
        query = query.where(func.coalesce(observed_at, keys.c.legacy_seen_at) >= seen_since)
    return query


def pending_daily_days(*criteria: ColumnElement[bool]):
    """Select pending calendar keys in SQL without loading any archived bodies.

    A changed projection, heart set or nearest time choice makes the whole day pending.
    Select current versions first: sightings choose A again even if B has a larger
    raw id; keys without observation evidence retain the legacy max-id fallback.
    A normalized-only historical day has no raw candidate and is left untouched.
    """
    raw = GarminRawRecord
    kinds = {kind for inputs in INPUTS.values() for kind in inputs}
    ids = current_record_ids(
        raw.kind.in_(kinds), func.length(raw.key) == 10, raw.key.like("____-__-__"), *criteria
    )
    return (
        select(raw.key)
        .where(
            raw.id.in_(ids),
            or_(
                daily_projection_pending(),
                heart_set_pending(),
                time_choice_pending(current_record_ids(raw.kind == "heart-rate")),
            ),
        )
        .distinct()
        .order_by(raw.key)
    )


async def newest_records(
    session: AsyncSession,
    kinds: Iterable[str],
    start: date,
    end: date,
    *,
    seen_since: datetime | None = None,
    record_ids: dict[tuple[str, str], int] | None = None,
) -> dict[str, dict[str, Any]]:
    """kind -> key -> last observed body; activities use observation time, days key range."""
    wanted = tuple(kinds)
    latest: dict[str, dict[str, Any]] = {kind: {} for kind in wanted}
    for activities in (False, True):
        names = [kind for kind in wanted if (kind == "activities") == activities]
        if not names:
            continue
        criteria = [GarminRawRecord.kind.in_(names)]
        if not activities:
            criteria += [
                GarminRawRecord.key >= start.isoformat(),
                GarminRawRecord.key <= end.isoformat(),
            ]
        ids = current_record_ids(
            *criteria, seen_since=seen_since if activities else None, activities=activities
        )
        rows = await session.execute(
            select(
                GarminRawRecord.kind, GarminRawRecord.key, GarminRawRecord.body, GarminRawRecord.id
            ).where(GarminRawRecord.id.in_(ids))
        )
        for kind, key, body, identifier in rows.tuples():
            if activities:
                key = activity_key(body) or key
            latest[kind][key] = body
            if record_ids is not None:
                record_ids[kind, key] = identifier
    return latest
