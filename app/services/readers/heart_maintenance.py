"""Explicit restart-safe initial fill (one ordered pass) and bounded per-hour repair."""

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.contracts import SessionFactory
from app.db.dialect import database, database_operation, dialect_insert
from app.models.health import HeartRate
from app.models.heart_invalidation import HEART_LOCK
from app.models.heart_projection import HeartHour, HeartProjectionState
from app.models.state import RawHealthData

from . import heart_fill, heart_projection
from .dashboard_sources import DashboardSources

logger = logging.getLogger(__name__)
BATCH = 16
# Commit about every day of packets; poll a held writer fence for up to two minutes.
FLUSH_HOURS = 24
FLUSH_SAMPLES = 20_000
FENCE_TRIES = 120
FENCE_WAIT = 1.0
SPACE_FACTOR = 1.4


def _after(bucket: datetime) -> datetime:
    """The exclusive end of an hour; the year-9999 hour ends at the largest datetime."""
    try:
        return bucket + heart_projection.HOUR
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


@database_operation
async def fence(session: AsyncSession) -> bool:
    if await session.scalar(text("SHOW transaction_isolation")) != "read committed":
        return False
    # Never pile up concurrent builders behind an ingestion or maintenance transaction.
    return bool(await session.scalar(text(f"SELECT pg_try_advisory_xact_lock({HEART_LOCK})")))


async def _pack(session: AsyncSession, bucket: datetime) -> list:
    raw = aliased(RawHealthData)
    rows = await session.stream(
        select(raw.id, raw.source_start_time, raw.raw_json)
        .select_from(HeartRate)
        .join(
            raw,
            and_(
                raw.data_type == "heart-rate",
                raw.google_data_point_id == HeartRate.google_data_point_id,
            ),
        )
        .where(
            raw.source_start_time >= bucket,
            raw.source_start_time < _after(bucket),
            HeartRate.is_quarantined.is_(False),
            DashboardSources().normalized(HeartRate, "heart-rate"),
        )
        # The id orders equal source times, so a rebuild and the fill write equal digests.
        .order_by(raw.source_start_time, raw.id)
        .execution_options(yield_per=100)
    )
    packed = []
    try:
        async for raw_id, source_time, record in rows:
            if entry := heart_fill.sample(raw_id, source_time, record):
                packed.append(entry)
    finally:
        await rows.close()
    return packed


async def _build(session: AsyncSession, bucket: datetime) -> None:
    await session.execute(
        dialect_insert(session, HeartHour).values(bucket=bucket).on_conflict_do_nothing()
    )
    row = await session.get(HeartHour, bucket, populate_existing=True)
    if row is None:
        raise RuntimeError("Missing registered heart hour")
    packed = await _pack(session, bucket)
    row.samples = packed
    row.digest = heart_projection.digest(packed)
    row.method = heart_projection.METHOD
    row.built_revision = row.revision
    await session.flush()


async def _initialize(session: AsyncSession) -> HeartProjectionState:
    state = await session.get(HeartProjectionState, 1, populate_existing=True)
    if state is not None:
        return state
    first, last = (
        await session.execute(
            select(
                func.min(RawHealthData.source_start_time), func.max(RawHealthData.source_start_time)
            ).where(RawHealthData.data_type == "heart-rate")
        )
    ).one()
    state = HeartProjectionState(
        id=1,
        ready=first is None,
        next_hour=heart_projection.hour(first) if first else None,
        stop=_after(heart_projection.hour(last)) if last else None,
    )
    session.add(state)
    await session.flush()
    return state


async def _start(sessions: SessionFactory) -> bool:
    """Capture the initial horizon under the writer fence; False while a writer holds it."""
    async with sessions.begin() as session:
        if not await fence(session):
            return False
        await _initialize(session)
    return True


@dataclass
class _Fill:
    sessions: SessionFactory
    seen: dict
    expected: datetime
    progress: heart_fill.FillProgress

    async def commit(self, batch: list, cursor: datetime) -> bool:
        """Write packets and advance the cursor in one short fenced transaction.

        The fence is tried, never queued for; a writer holding it is waited out by polling
        while the reader keeps its snapshot. False stops the fill at the last commit.
        """
        for _ in range(FENCE_TRIES):
            async with self.sessions.begin() as session:
                if await fence(session):
                    state = await session.get(HeartProjectionState, 1, populate_existing=True)
                    if state is None or state.ready or state.next_hour != self.expected:
                        return False  # another builder moved the cursor first
                    for bucket, packed in batch:
                        await heart_fill.write(session, bucket, packed, self.seen.get(bucket))
                    state.next_hour = cursor
                    state.ready = state.stop is not None and cursor >= state.stop
                    self.expected = cursor
                    self.progress.packets += len(batch)
                    return True
            await asyncio.sleep(FENCE_WAIT)
        return False


async def _stream(
    session: AsyncSession,
    fill: _Fill,
    stop: datetime,
    limit: int | None,
    halt: asyncio.Event | None,
) -> int:
    start = fill.progress.packets

    def written() -> int:
        return fill.progress.packets - start

    batch, samples, cursor = [], 0, stop
    async with aclosing(heart_fill.hours(session, fill.expected, stop)) as hours:
        async for bucket, packed in hours:
            stopping = halt is not None and halt.is_set()
            if stopping or limit is not None and written() + len(batch) >= limit:
                cursor = bucket  # the first hour not written
                break
            batch.append((bucket, packed))
            samples += len(packed)
            fill.progress.hours += 1
            if len(batch) >= FLUSH_HOURS or samples >= FLUSH_SAMPLES:
                if not await fill.commit(batch, _after(bucket)):
                    return written()
                batch, samples = [], 0
    await fill.commit(batch, cursor)
    return written()


async def backfill_heart_hours(
    sessions: SessionFactory,
    *,
    limit: int | None = None,
    progress: heart_fill.FillProgress | None = None,
    halt: asyncio.Event | None = None,
) -> int:
    """Maintenance only: fill the initial horizon in one ordered pass; returns packets written.

    The initial horizon is captured under the writer fence. One REPEATABLE READ snapshot
    supplies both the samples and the hour revisions; packets and the cursor commit in short
    fenced transactions, so an interruption resumes at the last commit. Later writes always
    enqueue their hours, including historical insertions before the cursor or outside that
    horizon. No packet is authoritative until the whole initial horizon commits as ready.
    ``limit`` bounds the hours written (default: all); ``halt`` stops after the current hour.
    """
    if limit is not None and limit < 1:
        raise ValueError("Saat sınırı en az 1 olmalı.")
    if not await _start(sessions):
        return 0
    async with sessions() as session:
        await database(session).control(
            session, text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        state = await session.get(HeartProjectionState, 1)
        if state is None or state.ready or state.next_hour is None or state.stop is None:
            return 0
        seen = await heart_fill.revisions(session, state.next_hour, state.stop)
        fill = _Fill(sessions, seen, state.next_hour, progress or heart_fill.FillProgress())
        return await _stream(session, fill, state.stop, limit, halt)


async def fill_space_needed(sessions: SessionFactory) -> int:
    """Bytes of temporary files the fill may need: its hash joins and source-time sort spill
    about the raw table's size; the factor leaves headroom."""
    async with sessions() as session:
        size = await session.scalar(text("SELECT pg_table_size('raw_health_data')"))
    return int((size or 0) * SPACE_FACTOR)


async def fill_is_ready(sessions: SessionFactory) -> bool:
    async with sessions() as session:
        return bool(
            await session.scalar(
                select(HeartProjectionState.ready).where(HeartProjectionState.id == 1)
            )
        )


async def refresh_heart_hours(sessions: SessionFactory, *, limit: int = BATCH) -> int:
    """Repair only queued hours, never discover or backfill an archive in the scheduler."""
    if not 1 <= limit <= 168:
        raise ValueError("Saat sınırı 1–168 arasında olmalı.")
    async with sessions() as session:
        ready = await session.scalar(
            select(HeartProjectionState.ready).where(HeartProjectionState.id == 1)
        )
        if not ready:
            return 0
        keys = list(
            await session.scalars(
                select(HeartHour.bucket)
                .where(
                    or_(
                        HeartHour.built_revision != HeartHour.revision,
                        HeartHour.method.is_distinct_from(heart_projection.METHOD),
                        HeartHour.samples.is_(None),
                        HeartHour.digest.is_(None),
                    )
                )
                # Newest first: the dashboard reads recent hours; an old backlog waits.
                .order_by(HeartHour.bucket.desc())
                .limit(limit)
            )
        )
    processed = 0
    for bucket in keys:
        try:
            async with sessions.begin() as session:
                if not await fence(session):
                    break
                row = await session.get(HeartHour, bucket, populate_existing=True)
                if (
                    row is not None
                    and heart_projection.unpack(row, bucket, _after(bucket)) is not None
                ):
                    continue
                await _build(session, bucket)
                processed += 1
        except Exception as exc:
            # Rollback leaves this hour dirty. Its reader uses raw data, while a
            # malformed hour cannot prevent other hours or daily refresh from running.
            logger.warning("Heart hour refresh failed: %s", type(exc).__name__)
    return processed
