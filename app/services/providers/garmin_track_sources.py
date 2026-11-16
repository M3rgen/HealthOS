"""Which ready track package belongs to a FIT-backed workout; no file body is loaded.

A Garmin API workout (``activities:<key>``) uses its activity's current original file:
the newest ``downloaded`` result in ``garmin_activity_file_results``. A manual upload's
workout (``fit:<record key>``) uses the link derived from its upload
(``garmin_track_sessions``); if several uploads produced the same workout, the most
recently received upload wins. Only packages of the current method with status
``ready`` and at least one sample count, so ``has_track`` and the track endpoint agree.
"""

import hashlib
import uuid
from base64 import urlsafe_b64decode
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.fit import FIT_FILE, FIT_RECORD_PREFIX
from app.models.garmin_files import GarminActivityFile, GarminActivityFileResult
from app.models.garmin_tracks import GarminTrack, GarminTrackSession
from app.models.health import ExerciseSession
from app.models.state import RawHealthData

from .fit_tracks import METHOD_VERSION

if TYPE_CHECKING:
    from app.services.readers.reading import WorkoutRecord

ACTIVITY_PREFIX = "activities:"
LONG_KEY_PREFIX = "sha256:"
_BATCH = 500


@dataclass(frozen=True)
class StoredTrack:
    """One ready package as stored: compressed column sets and the small summary."""

    samples: bytes
    laps: bytes
    events: bytes
    summary: dict
    sample_count: int


def activity_digest(source_record_id: str) -> str | None:
    """``garmin_activity_file_results.activity_sha256`` of a Garmin API workout.

    A long identifier's key already carries its SHA-256 (``activity_key``); a short key
    is the identifier itself.
    """
    if not source_record_id.startswith(ACTIVITY_PREFIX):
        return None
    key = source_record_id.removeprefix(ACTIVITY_PREFIX)
    if key.startswith(LONG_KEY_PREFIX):
        encoded = key.removeprefix(LONG_KEY_PREFIX)
        try:
            digest = urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError:
            return None
        return digest.hex() if len(digest) == 32 else None
    return hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()


def _ready(digest: InstrumentedAttribute[str] | ColumnElement[str]):
    return (
        select(GarminTrack.source_sha256)
        .where(
            GarminTrack.source_sha256 == digest,
            GarminTrack.method_version == METHOD_VERSION,
            GarminTrack.status == "ready",
            GarminTrack.sample_count > 0,
        )
        .exists()
    )


async def _downloaded(session: AsyncSession, activities: list[str]) -> dict[str, str]:
    """activity digest -> content digest of its current file, when that file has a track."""
    result, files = GarminActivityFileResult, GarminActivityFile
    found: dict[str, str] = {}
    for start in range(0, len(activities), _BATCH):
        current = (
            select(result.activity_sha256, func.max(result.id).label("id"))
            .where(
                result.activity_sha256.in_(activities[start : start + _BATCH]),
                result.status == "downloaded",
            )
            .group_by(result.activity_sha256)
            .subquery()
        )
        rows = await session.execute(
            select(current.c.activity_sha256, files.sha256)
            .join(result, result.id == current.c.id)
            .join(files, files.id == result.file_id)
            .where(_ready(files.sha256))
        )
        found.update((activity, content) for activity, content in rows.tuples())
    return found


async def _uploaded(session: AsyncSession, keys: list[str]) -> dict[str, str]:
    """FIT workout key -> content digest of its newest linked upload with a track."""
    link, raw = GarminTrackSession, RawHealthData
    found: dict[str, str] = {}
    for start in range(0, len(keys), _BATCH):
        rows = await session.execute(
            select(link.source_record_id, link.source_sha256)
            .join(
                raw,
                (raw.data_type == FIT_FILE.name)
                & (raw.google_data_point_id == "garmin-fit:file:" + link.source_sha256),
            )
            .where(link.source_record_id.in_(keys[start : start + _BATCH]))
            .where(_ready(link.source_sha256))
            .order_by(link.source_record_id, raw.last_seen_at, link.source_sha256)
        )
        # Ordered: the newest upload of each workout is written last.
        found.update((key, content) for key, content in rows.tuples())
    return found


async def track_sources(
    session: AsyncSession, workouts: Mapping[str, str | None]
) -> dict[str, str]:
    """workout id -> content digest of its ready package, for workouts that have one.

    ``workouts`` maps a workout id to its ``source_record_id``. At most one query per
    500 workouts of each kind; nothing depends on the size of a track.
    """
    activities = {
        key: digest
        for key, source in workouts.items()
        if source and (digest := activity_digest(source)) is not None
    }
    uploads = {
        key: source
        for key, source in workouts.items()
        if source and source.startswith(FIT_RECORD_PREFIX)
    }
    found: dict[str, str] = {}
    if activities:
        files = await _downloaded(session, sorted(set(activities.values())))
        found.update((key, files[digest]) for key, digest in activities.items() if digest in files)
    if uploads:
        files = await _uploaded(session, sorted(set(uploads.values())))
        found.update((key, files[source]) for key, source in uploads.items() if source in files)
    return found


async def stored_track(session: AsyncSession, digest: str) -> StoredTrack | None:
    """The current method's ready package for this content; one query."""
    row = (
        await session.execute(
            select(
                GarminTrack.samples,
                GarminTrack.laps,
                GarminTrack.events,
                GarminTrack.summary,
                GarminTrack.sample_count,
            ).where(
                GarminTrack.source_sha256 == digest,
                GarminTrack.method_version == METHOD_VERSION,
                GarminTrack.status == "ready",
                GarminTrack.sample_count > 0,
            )
        )
    ).one_or_none()
    return StoredTrack(*row) if row is not None else None


async def mark_tracks(session: AsyncSession, rows: "list[WorkoutRecord]") -> "list[WorkoutRecord]":
    """Set ``has_track`` on FIT-backed workout rows; one key query plus ``track_sources``.

    Kept apart from the workout query itself, which stays a single bounded stream.
    """
    keys: dict[str, str | None] = {}
    ids = [uuid.UUID(row["id"]) for row in rows]
    for start in range(0, len(ids), _BATCH):
        found = await session.execute(
            select(ExerciseSession.id, ExerciseSession.source_record_id).where(
                ExerciseSession.id.in_(ids[start : start + _BATCH])
            )
        )
        keys.update((str(key), source) for key, source in found.tuples())
    tracks = await track_sources(session, keys) if keys else {}
    for row in rows:
        row["has_track"] = row["id"] in tracks
    return rows
