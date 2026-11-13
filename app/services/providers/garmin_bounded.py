"""Garmin normalization in bounded transactions.

A backfill can cover years. One transaction per ``CHUNK_DAYS`` calendar days holds one newest
body per kind and day, and activities (keyed by id, read by page of keys) go in their own short
transactions. A run interrupted between two commits leaves whole chunks; running it again
writes only what is missing or changed (``write_rows``). Daily heart membership commits with
its chunk, including empty sets; an interrupted chunk preserves the previous complete view.
Periodic pulls additionally select pending archive days in SQL, comparing current raw ids
with completed daily projections and heart sets. Each recovery transaction handles at most
``CHUNK_DAYS`` selected days, independent of the distance between their calendar dates.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.contracts import TransactionSessions
from app.models.garmin import GarminRawRecord
from app.models.health import ExerciseSession

from .garmin_activity_identity import activity_key
from .garmin_activity_state import inherit_activity_quarantine, write_activity_results
from .garmin_current import activity_identity, current_record_ids, pending_daily_days
from .garmin_normalize import build_activity_rows, normalize_window, write_rows
from .garmin_normalize_types import NormalizeStats

CHUNK_DAYS = 7
ACTIVITIES_PER_TRANSACTION = 500


def day_chunks(start: date, end: date, days: int = CHUNK_DAYS) -> Iterator[tuple[date, date]]:
    """Inclusive chunks of at most ``days`` days covering ``start``..``end``."""
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


@dataclass(frozen=True)
class ActivityWindow:
    """Explicit date range, or periodic sightings plus pending archive work.

    With ``seen_since``, activity conversion is independent of the daily window:
    a crash must not strand archived work when the next pull moves past its date.
    """

    start: date
    end: date
    device: str
    seen_since: datetime | None = None


async def normalize_activities_page(
    session: AsyncSession,
    window: ActivityWindow,
    *,
    after: str | None,
    size: int,
) -> tuple[NormalizeStats, str | None]:
    """Page by full identity, including old raw keys shared by multiple activities."""
    identity = activity_identity()
    criteria = [GarminRawRecord.kind == "activities"]
    if after is not None:
        criteria.append(identity > after)
    ids = current_record_ids(
        *criteria, seen_since=window.seen_since, activities=True, include_pending=True
    )
    keys = (
        await session.execute(
            select(identity, GarminRawRecord.id)
            .where(GarminRawRecord.id.in_(ids))
            .order_by(identity)
            .limit(size)
        )
    ).all()
    stats = NormalizeStats()
    if not keys:
        return stats, None
    selected = await session.execute(
        select(GarminRawRecord.key, GarminRawRecord.body, GarminRawRecord.id).where(
            GarminRawRecord.id.in_([identifier for _, identifier in keys])
        )
    )
    bodies: dict[str, Any] = {}
    record_ids = {}
    for key, body, identifier in selected.tuples():
        key = activity_key(body) or key
        bodies[key] = body
        record_ids["activities", key] = identifier
    activity_results: dict[str, str | None] = {}
    rows, stats.skipped = build_activity_rows(
        bodies,
        window.start,
        window.end,
        window.device,
        activity_results=activity_results,
        restrict_window=window.seen_since is None,
    )
    page = sorted(rows, key=lambda row: row["google_data_point_id"])
    if page:
        await inherit_activity_quarantine(session, page, bodies)
        inserted, updated, unchanged = await write_rows(session, ExerciseSession, page)
        stats.note(ExerciseSession.__tablename__, inserted, updated, unchanged)
    await write_activity_results(session, record_ids, activity_results)
    return stats, keys[-1][0] if len(keys) == size else None


async def normalize_bounded(
    sessions: TransactionSessions,
    start: date,
    end: date,
    *,
    device: str,
    seen_since: datetime | None = None,
) -> NormalizeStats:
    """Normalize days and activities through the caller's transaction capability.

    Each begin revalidates ownership when supplied by a pull. Never fall back to a
    pooled factory here: completed chunks survive a lost owner, while its next chunk
    cannot publish values or their projection/conversion results. Both still commit
    together inside normalize_window and normalize_activities_page.
    """
    total = NormalizeStats()
    for chunk_start, chunk_end in day_chunks(start, end, days=CHUNK_DAYS):
        async with sessions.begin() as session:
            total.add(
                await normalize_window(
                    session, chunk_start, chunk_end, device=device, activities=False
                )
            )
    window = ActivityWindow(start, end, device, seen_since)
    after: str | None = None
    while True:
        async with sessions.begin() as session:
            stats, after = await normalize_activities_page(
                session, window, after=after, size=ACTIVITIES_PER_TRANSACTION
            )
        total.add(stats)
        if after is None:
            if seen_since is not None:
                total.add(await recover_daily(sessions, start, end, device=device))
            return total


async def recover_daily(
    sessions: TransactionSessions, start: date, end: date, *, device: str
) -> NormalizeStats:
    """Finish pending archive days outside the periodic window in bounded transactions.

    Page calendar keys, not payloads. Sparse days never expand into an unbounded
    date range or reload completed days between them. Values, daily projections and
    heart membership use the same converter and ownership fence as the normal window.
    Explicit normalization/backfills keep their requested range; periodic pulls alone
    drain this backlog, even when all subsequent HTTP responses contain no records.
    """
    total = NormalizeStats()
    after = ""
    while True:
        async with sessions.begin() as session:
            keys = list(
                await session.scalars(
                    pending_daily_days(
                        GarminRawRecord.key > after,
                        or_(
                            GarminRawRecord.key < start.isoformat(),
                            GarminRawRecord.key > end.isoformat(),
                        ),
                    ).limit(CHUNK_DAYS)
                )
            )
            for key in keys:
                try:
                    day = date.fromisoformat(key)
                except ValueError:
                    # Malformed calendar keys stay archived, outside the converter's domain.
                    continue
                total.add(
                    await normalize_window(session, day, day, device=device, activities=False)
                )
        if len(keys) < CHUNK_DAYS:
            return total
        after = keys[-1]
