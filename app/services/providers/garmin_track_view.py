"""The track of one FIT-backed workout, as streamed by ``GET /api/dashboard/workouts/{id}/track``.

A workout is found only where the workout list shows it (same provider reader, so the
same version, quarantine and publication rules); the type comes from that row, never
from the package. The response holds no name, device, file or place, and nothing here
logs a value: errors carry fixed messages.
"""

import json
import math
import uuid
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import FIT, GARMIN
from app.core.timeutil import as_utc
from app.models.health import ExerciseSession

from .fit_track_columns import json_object, values
from .fit_track_thinning import sample_rows, thin
from .fit_tracks import METHOD_VERSION
from .garmin_readers import workouts
from .garmin_track_sources import StoredTrack, stored_track, track_sources

TRACK_PROVIDERS = (GARMIN, FIT)
MIN_POINTS = 100


class TrackNotFound(LookupError):
    """No FIT-backed workout with this id is listed."""

    def __init__(self) -> None:
        super().__init__("Antrenman bulunamadı / Workout not found")


@dataclass(frozen=True)
class WorkoutTrack:
    workout_id: str
    exercise_type: str | None
    stored: StoredTrack


@dataclass(frozen=True)
class TrackPlan:
    """What a response keeps: sample rows (None = all) and each lap's row positions."""

    rows: list[int] | None
    laps: list[tuple[str, list[int | None]]] = field(default_factory=list)


async def workout_track(session: AsyncSession, workout_id: uuid.UUID) -> WorkoutTrack | None:
    """The listed workout's ready track, None when it has none; ``TrackNotFound`` otherwise."""
    found = (
        await session.execute(
            select(
                ExerciseSession.provider,
                ExerciseSession.start_time,
                ExerciseSession.source_record_id,
            ).where(ExerciseSession.id == workout_id, ExerciseSession.provider.in_(TRACK_PROVIDERS))
        )
    ).one_or_none()
    if found is None:
        raise TrackNotFound()
    provider, start, source = found
    start = as_utc(start)
    key = str(workout_id)
    listed = [
        row
        for row in await workouts(session, start, start + timedelta(microseconds=1), provider)
        if row["id"] == key
    ]
    if not listed:
        raise TrackNotFound()
    digest = (await track_sources(session, {key: source})).get(key)
    stored = await stored_track(session, digest) if digest is not None else None
    if stored is None:
        return None
    return WorkoutTrack(key, listed[0]["exercise_type"], stored)


def _position(row: int | None, rows: list[int] | None) -> int | None:
    """A sample row's index in the response's arrays (the next kept row when thinned)."""
    if row is None or rows is None:
        return row
    return min(bisect_left(rows, row), len(rows) - 1)


def plan_track(stored: StoredTrack, max_points: int | None) -> TrackPlan:
    """Choose the rows to send and place every lap boundary on them (CPU only, no I/O)."""
    starts = list(values(stored.laps, "start_time"))
    ends = list(values(stored.laps, "time"))
    found = sample_rows(stored.samples, [*starts, *ends], stored.sample_count)
    rows = None
    if max_points is not None:
        rows = thin(stored.samples, stored.sample_count, found.values(), max_points)

    def placed(moments: list[float]) -> list[int | None]:
        return [None if math.isnan(at) else _position(found.get(at), rows) for at in moments]

    return TrackPlan(rows, [("start_index", placed(starts)), ("end_index", placed(ends))])


def track_document(track: WorkoutTrack, plan: TrackPlan) -> Iterator[str]:
    """``{"track": {…}}`` in bounded text chunks; arrays share the plan's sample rows."""
    stored = track.stored
    head = {
        "workout_id": track.workout_id,
        "exercise_type": track.exercise_type,
        "method_version": METHOD_VERSION,
        "summary": stored.summary,
        "source_point_count": stored.sample_count,
        "point_count": stored.sample_count if plan.rows is None else len(plan.rows),
    }
    yield '{"track":' + json.dumps(head, allow_nan=False)[:-1] + ',"samples":'
    yield from json_object(stored.samples, plan.rows)
    yield ',"laps":'
    yield from json_object(stored.laps, extra=plan.laps)
    yield ',"events":'
    yield from json_object(stored.events)
    yield "}}"
