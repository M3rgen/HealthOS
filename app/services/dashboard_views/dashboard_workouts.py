"""Manual and device workout reads, writes and source placement for the dashboard."""

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import APPLE, GOOGLE
from app.core.timeutil import as_utc
from app.models.health import ExerciseSession
from app.models.identity_queries import plan_each_query
from app.models.manual import ManualWorkout
from app.schemas.dashboard import Workout
from app.services import identities, providers
from app.services.identities.source_precedence import (
    MANUAL,
    PRIMARY,
    Observation,
    Policy,
    label,
    resolve_sessions,
)
from app.services.providers.ingestion_readers import ARCHIVES
from app.services.readers import Archives
from app.services.readers.dashboard_window import monthly_window
from app.services.readers.manual_validation import validate_workout


def workout_dict(row: ManualWorkout | ExerciseSession, source: str) -> Workout:
    """A manual workout as the API lists it (``source`` is the display label, "Manuel")."""
    duration = getattr(row, "duration_seconds", None)
    return {
        "id": str(row.id),
        "exercise_type": row.exercise_type,
        "start_time": as_utc(row.start_time),
        "end_time": as_utc(row.end_time),
        "duration_seconds": (
            float(duration)
            if source != "Manuel" and duration is not None
            else (as_utc(row.end_time) - as_utc(row.start_time)).total_seconds()
        ),
        "calories_kcal": float(row.calories_kcal) if row.calories_kcal is not None else None,
        "distance_meters": float(row.distance_meters) if row.distance_meters is not None else None,
        "source": source,
        "source_code": MANUAL,
        "provider": MANUAL,
        "priority": PRIMARY,
        "group": None,
        "alternatives": [],
    }


def relayed_apple_workouts(rows: list[dict]) -> dict[str, str]:
    """Apple workouts Google also lists: key of the Apple row -> key of the Google row.

    Google relays Apple Health data with the source code ``HEALTH_KIT``, the code Apple's own
    rows carry, so ``resolve_sessions`` (which never sets one source against itself) would
    list both. An Apple row that overlaps a Google ``HEALTH_KIT`` row by at least half of the
    shorter of the two is the same workout: it is folded under that row (the first such by
    start time) instead of being listed again.
    """
    google = sorted(
        (
            row
            for row in rows
            if row["provider"] == GOOGLE
            and row["source_code"] == providers.apple_readers.HEALTH_KIT
        ),
        key=lambda row: (row["start_time"], str(row["id"])),
    )
    folded: dict[str, str] = {}
    for row in rows:
        if row["provider"] != APPLE:
            continue
        match = next(
            (
                other
                for other in google
                if providers.apple_readers.overlap_share(
                    row["start_time"], row["end_time"], other["start_time"], other["end_time"]
                )
                >= providers.apple_readers.SAME_WORKOUT
            ),
            None,
        )
        if match is not None:
            folded[_workout_key(row)] = _workout_key(match)
    return folded


def _workout_key(row: dict) -> str:
    return f"{row['source_code']}:{row['provider']}:{row['id']}"


def _alternative(row: dict) -> dict:
    return {
        "id": row["id"],
        "source": label(row["source_code"]),
        "source_code": row["source_code"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
    }


def place_workouts(rows: list[dict], policy: Policy | None = None) -> list[dict]:
    """Label every row's source and place the device rows under the activity policy: a
    session that overlaps a higher-ranked source's session is ``secondary`` and listed among
    that session's ``alternatives``; nothing is merged or summed. Manual rows stay as they are.
    An Apple workout Google also relays (``relayed_apple_workouts``) is ``secondary`` under
    the Google row (or under the row that Google row is itself folded under).
    """
    policy = policy or Policy.from_settings()
    folded = relayed_apple_workouts(rows)
    placed = [row for row in rows if _workout_key(row) not in folded]
    placements = resolve_sessions(
        [
            Observation(
                row["source_code"],
                row,
                provider=row["provider"],
                record_id=_workout_key(row),
                start=row["start_time"],
                end=row["end_time"],
            )
            for row in placed
        ],
        policy,
    )
    by_key = {_workout_key(row): row for row in rows}
    for key, row in by_key.items():
        if key in folded:
            continue
        placement = placements[key]
        row["source"] = label(row["source_code"])
        row["priority"] = placement.priority
        row["group"] = (
            by_key[placement.group]["id"] if placement.group in by_key else placement.group
        )
        row["alternatives"] = [_alternative(item.value) for item in placement.alternatives]
    for key, holder_key in folded.items():
        row, holder = by_key[key], by_key[holder_key]
        primary = holder
        if holder["priority"] == identities.source_precedence.SECONDARY:
            primary = next((other for other in placed if other["id"] == holder["group"]), holder)
        row["source"] = label(row["source_code"])
        row["priority"] = identities.source_precedence.SECONDARY
        row["group"] = primary["id"]
        row["alternatives"] = []
        primary["alternatives"].append(_alternative(row))
    return rows


async def create_workout(session: AsyncSession, values: dict[str, Any]) -> Workout:
    validate_workout(values)
    item = ManualWorkout(**values)
    session.add(item)
    await session.flush()
    await session.refresh(item)
    return workout_dict(item, "Manuel")


async def list_workouts(
    session: AsyncSession, first: date, zone: ZoneInfo, *, archives: Archives = ARCHIVES
) -> list[Workout]:
    window = monthly_window(first, zone)
    start, stop = window.start, window.stop
    await plan_each_query(session)
    rows = []
    for archive in archives:
        if archive.workouts is not None:
            rows.extend(await archive.workouts(session, start, stop))
    manual = (
        await session.scalars(
            select(ManualWorkout).where(
                ManualWorkout.start_time >= start,
                ManualWorkout.start_time < stop,
            )
        )
    ).all()
    rows.extend(workout_dict(row, "Manuel") for row in manual)
    place_workouts(rows)
    return sorted(rows, key=lambda row: row["start_time"], reverse=True)
