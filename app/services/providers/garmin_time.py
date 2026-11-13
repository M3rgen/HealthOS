"""Civil-day bounds and UTC offsets evidenced by Garmin heart-rate records and adjacent days."""

from bisect import bisect_left
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.normalization import civil_bounds, parse_datetime
from app.models.garmin import GarminRawRecord

from .garmin_current import current_record_ids


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
    position = bisect_left(zones, day, key=lambda item: item[0])
    candidates = zones[max(0, position - 1) : position + 1]
    # Earlier day wins a distance tie. Never infer an IANA timezone from a UTC offset.
    nearest = min(candidates, key=lambda item: (abs((item[0] - day).days), item[0]), default=None)
    return nearest[1] if nearest else None


def zone_label(day: date, zone: timezone) -> str:
    value = datetime.combine(day, time.min, zone).strftime("%z")
    return f"{value[:3]}:{value[3:]}"


async def adjacent_hearts(session: AsyncSession, start: date, end: date) -> dict[str, Any]:
    """Read the nearest usable day on either side, skipping empty/invalid latest bodies."""
    found = {}
    for boundary, before in ((start, True), (end, False)):
        cursor = str(boundary)
        while True:
            key_column = GarminRawRecord.key
            key = await session.scalar(
                select(key_column)
                .where(
                    GarminRawRecord.kind == "heart-rate",
                    key_column < cursor if before else key_column > cursor,
                )
                .order_by(key_column.desc() if before else key_column.asc())
                .limit(1)
            )
            if key is None:
                break
            body = await session.scalar(
                select(GarminRawRecord.body).where(
                    GarminRawRecord.id.in_(
                        current_record_ids(
                            GarminRawRecord.kind == "heart-rate", GarminRawRecord.key == key
                        )
                    )
                )
            )
            cursor = key
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
            if heart_zone(day, body) is not None:
                found[key] = body
                break
    return found
