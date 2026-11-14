"""Civil-day bounds and UTC offsets evidenced by Garmin heart-rate records and adjacent days."""

from bisect import bisect_left
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import Select, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.normalization import civil_bounds, parse_datetime
from app.models.garmin import GarminRawRecord, GarminTimeEvidence

from .garmin_current import current_record_ids

# Rows requested per fetch. On PostgreSQL SQLAlchemy's asyncpg cursor fetches at
# least 50 rows whatever yield_per asks, so up to 50 raw bodies are decoded at once.
_ADJACENT_BATCH = 16


def day_bounds(day: date, heart_rate: Any, zone: timezone = UTC) -> tuple[datetime, datetime]:
    """The heart-rate day's half-open bounds, or the evidenced civil day's bounds.

    An inclusive end can parse successfully yet overflow when advanced a second.
    Treat that as an unusable interval, just like missing or reversed bounds;
    retain the caller's evidenced offset without clipping either timestamp.
    """
    if isinstance(heart_rate, dict):
        start = parse_datetime(heart_rate.get("startTimestampGMT"))
        end = parse_datetime(heart_rate.get("endTimestampGMT"))
        if start is not None and end is not None and end > start:
            try:
                # Keep Garmin's existing last-second rule, including fractional seconds.
                end = end + timedelta(seconds=1) if end.second == 59 else end
            except OverflowError:
                # The calendar day remains usable even when the raw end is not.
                pass
            else:
                return start, end
    return civil_bounds(day, zone)


def heart_zone(day: date, body: Any) -> timezone | None:
    if not isinstance(body, dict):
        return None
    start = parse_datetime(body.get("startTimestampGMT"))
    end = parse_datetime(body.get("endTimestampGMT"))
    if start is None or end is None or end <= start:
        return None
    offset = datetime.combine(day, time.min, UTC) - start
    if abs(offset) > timedelta(hours=14) or offset.total_seconds() % 60:
        return None
    return timezone(offset)


def day_zones(hearts: dict[str, Any]) -> list[tuple[date, timezone]]:
    found = []
    for key, body in hearts.items():
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        zone = heart_zone(day, body)
        if zone is not None:
            found.append((day, zone))
    return sorted(found)


def nearest_zone(day: date, zones: list[tuple[date, timezone]]) -> timezone | None:
    nearest = nearest_evidence(day, zones)
    return nearest[1] if nearest else None


def nearest_evidence(day: date, zones: list[tuple[date, timezone]]) -> tuple[date, timezone] | None:
    position = bisect_left(zones, day, key=lambda item: item[0])
    candidates = zones[max(0, position - 1) : position + 1]
    # Earlier day wins a distance tie. Never infer an IANA timezone from a UTC offset.
    nearest = min(candidates, key=lambda item: (abs((item[0] - day).days), item[0]), default=None)
    return nearest


def zone_label(day: date, zone: timezone) -> str:
    value = datetime.combine(day, time.min, zone).strftime("%z")
    return f"{value[:3]}:{value[3:]}"


def _adjacent_query(boundary: date, *, before: bool) -> Select:
    """One current version per key outside the window, nearest keys first."""
    key = GarminRawRecord.key
    ids = current_record_ids(
        GarminRawRecord.kind == "heart-rate",
        key < str(boundary) if before else key > str(boundary),
    )
    return (
        select(GarminRawRecord.id, key, GarminRawRecord.body)
        .where(GarminRawRecord.id.in_(ids))
        .order_by(key.desc() if before else key.asc())
        .execution_options(yield_per=_ADJACENT_BATCH)
    )


async def adjacent_hearts(
    session: AsyncSession,
    start: date,
    end: date,
    *,
    record_ids: dict[tuple[str, str], int] | None = None,
) -> dict[str, Any]:
    """Read each side in bounded batches, stopping at its first usable current body.

    Select versions in SQL before streaming: an observation may return to older
    content, while unobserved keys retain the legacy max-id choice. Only the two
    winning bodies survive the streams, regardless of the number of invalid days.
    Closing a result also releases its unread batch without ending the transaction.
    """
    found = {}
    for boundary, before in ((start, True), (end, False)):
        rows = await session.stream(_adjacent_query(boundary, before=before))
        try:
            async for identifier, key, body in rows.tuples():
                try:
                    day = date.fromisoformat(key)
                except ValueError:
                    continue
                if heart_zone(day, body) is not None:
                    found[key] = body
                    if record_ids is not None:
                        record_ids["heart-rate", key] = identifier
                    break
        finally:
            await rows.close()
    return found


async def index_time_evidence(session: AsyncSession, size: int) -> bool:
    """Cache current heart versions in bounded batches; legacy/invalid keys cannot block it.

    Old versions and decisions remain intact. Reobservation selects an existing cache row;
    a changed payload is indexed once. No observed timestamp is fabricated for legacy data.
    Return whether a batch was indexed, so recovery starts in a separate transaction.
    """
    raw, evidence = GarminRawRecord, GarminTimeEvidence
    ids = current_record_ids(raw.kind == "heart-rate")
    rows = (
        await session.execute(
            select(raw.id)
            .where(raw.id.in_(ids), ~raw.id.in_(select(evidence.raw_record_id)))
            .order_by(raw.key)
            .limit(size)
        )
    ).all()
    if rows:
        selected = await session.execute(
            select(raw.id, raw.key, raw.body).where(raw.id.in_([row.id for row in rows]))
        )
        await cache_time_evidence(session, selected.all())
    return bool(rows)


async def cache_time_evidence(session: AsyncSession, rows: Iterable[Sequence[Any]]) -> None:
    """Store only previously unseen immutable heart versions, never re-write evidence."""
    evidence = GarminTimeEvidence
    rows = list(rows)
    if not rows:
        return
    existing = set(
        await session.scalars(
            select(evidence.raw_record_id).where(
                evidence.raw_record_id.in_([identifier for identifier, _, _ in rows])
            )
        )
    )
    values = []
    for identifier, key, body in rows:
        if identifier in existing:
            continue
        try:
            day = date.fromisoformat(key)
        except ValueError:
            day = None
        zone = heart_zone(day, body) if day is not None else None
        values.append(
            {
                "raw_record_id": identifier,
                "day_ordinal": day.toordinal() if day is not None else None,
                "offset_minutes": int(zone.utcoffset(None).total_seconds() / 60) if zone else None,
            }
        )
    if values:
        await session.execute(insert(evidence), values)
