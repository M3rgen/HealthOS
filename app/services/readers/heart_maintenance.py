"""Explicit restart-safe initial fill and bounded scheduler repair, one UTC hour at a time."""

import logging
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.contracts import SessionFactory
from app.db.dialect import database_operation, dialect_insert
from app.models.health import HeartRate
from app.models.heart_invalidation import HEART_LOCK
from app.models.heart_projection import HeartHour, HeartProjectionState
from app.models.state import RawHealthData

from . import current_heart, heart_projection
from .dashboard_sources import DashboardSources

logger = logging.getLogger(__name__)
BATCH = 16


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
        .order_by(raw.source_start_time)
        .execution_options(yield_per=100)
    )
    packed = []
    try:
        async for raw_id, source_time, record in rows:
            measurement = current_heart.current_heart_rate(record)
            if measurement is not None:
                moment, bpm = measurement
                source = record.get("dataSource")
                platform = source.get("platform") if isinstance(source, dict) else None
                # Hex preserves the original binary float, including signed zero, without
                # passing through Numeric(10,3) or JSONB's numeric canonicalization.
                packed.append(
                    [str(raw_id), source_time.isoformat(), moment.isoformat(), bpm.hex(), platform]
                )
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


async def _following(session: AsyncSession, after: datetime, stop: datetime) -> datetime:
    """The next hour holding a heart raw row. An empty hour needs no packet (its reader
    reads the empty raw hour), so one outlier timestamp cannot stretch the fill by years."""
    found = await session.scalar(
        select(func.min(RawHealthData.source_start_time)).where(
            RawHealthData.data_type == "heart-rate",
            RawHealthData.source_start_time >= after,
            RawHealthData.source_start_time < stop,
        )
    )
    return stop if found is None else heart_projection.hour(found)


async def backfill_heart_hours(sessions: SessionFactory, *, limit: int = BATCH) -> int:
    """Maintenance only: at most 168 hours per call; committed cursor resumes exactly.

    The initial horizon is captured under the writer fence. Later writes always enqueue
    their hours, including historical insertions before the cursor or outside that horizon.
    No packet is authoritative until the whole initial horizon commits as ready.
    """
    if not 1 <= limit <= 168:
        raise ValueError("Saat sınırı 1–168 arasında olmalı.")
    processed = 0
    for _ in range(limit):
        async with sessions.begin() as session:
            if not await fence(session):
                break
            state = await _initialize(session)
            if state.ready or state.next_hour is None or state.stop is None:
                break
            await _build(session, state.next_hour)
            next_hour = await _following(session, _after(state.next_hour), state.stop)
            state.next_hour = next_hour
            state.ready = next_hour >= state.stop
            processed += 1
    return processed


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
