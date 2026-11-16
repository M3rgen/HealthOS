"""Original FIT storage and download eligibility; all queries avoid loading file bodies."""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dialect import dialect_insert
from app.models.garmin import GarminRawRecord
from app.models.garmin_files import GarminActivityFile, GarminActivityFileResult
from app.schemas.status import GarminActivityFiles

from .garmin_activity_identity import activity_identifier_sql
from .garmin_current import current_activity_ids
from .garmin_tracks import refresh_track


def activity_states():
    raw, result = GarminRawRecord, GarminActivityFileResult
    identifier = activity_identifier_sql(raw.body)
    missing = identifier.in_(
        select(result.activity_id).where(result.status.in_(("not_found", "gone")))
    )
    checked = exists().where(result.raw_record_id == raw.id)
    complete = checked.where(result.status == "downloaded")
    return (
        select(
            raw.id.label("raw_id"),
            identifier.label("activity_id"),
            # An invalid file is settled for this list version only; a new version is pending.
            case(
                (missing, "unavailable"),
                (complete, "downloaded"),
                (checked, "invalid"),
                else_="pending",
            ).label("state"),
        )
        .where(raw.id.in_(current_activity_ids()), identifier.is_not(None), identifier != "")
        .subquery()
    )


async def file_counts(session: AsyncSession) -> GarminActivityFiles:
    states = activity_states()
    counts = {
        state: count
        for state, count in await session.execute(
            select(states.c.state, func.count()).group_by(states.c.state)
        )
    }
    return {
        "downloaded": counts.get("downloaded", 0),
        "pending": counts.get("pending", 0),
        "unavailable": counts.get("unavailable", 0),
        "invalid": counts.get("invalid", 0),
    }


@dataclass(frozen=True)
class FileOutcome:
    """What one download established: a FIT, a permanent absence or a refused body.

    ``invalid`` keeps only the refusal code and, for a completely received body, its
    digest and size; the refused bytes themselves are never stored.
    """

    status: str
    content: bytes | None = None
    reason: str | None = None
    content_sha256: str | None = None
    content_size: int | None = None


async def store_file_result(
    session: AsyncSession, raw_id: int, identifier: str, outcome: FileOutcome, now: datetime
) -> None:
    """Append one result with its file atomically; repeat bytes reuse the immutable version.

    The full source identity is preserved. Index only its digest so arbitrarily long
    legacy identifiers cannot exceed PostgreSQL's index-entry limit.
    """
    identity = {
        "activity_id": identifier,
        "activity_sha256": hashlib.sha256(identifier.encode()).hexdigest(),
        "raw_record_id": raw_id,
        "fetched_at": now,
    }
    file_id, content = None, outcome.content
    if content is not None:
        digest = hashlib.sha256(content).hexdigest()
        await session.execute(
            dialect_insert(session, GarminActivityFile)
            .values(**identity, body=content, sha256=digest, size=len(content))
            .on_conflict_do_nothing(index_elements=["activity_sha256", "sha256"])
        )
        file_id = await session.scalar(
            select(GarminActivityFile.id).where(
                GarminActivityFile.activity_sha256 == identity["activity_sha256"],
                GarminActivityFile.sha256 == digest,
            )
        )
    if content is not None and file_id is not None:
        await refresh_track(session, content)
    await session.execute(
        dialect_insert(session, GarminActivityFileResult)
        .values(
            **identity,
            file_id=file_id,
            status=outcome.status,
            reason=outcome.reason,
            content_sha256=outcome.content_sha256,
            content_size=outcome.content_size,
        )
        .on_conflict_do_nothing(index_elements=["raw_record_id"])
    )
