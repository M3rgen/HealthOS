"""Read validated packets only; incomplete, stale or opaque reads stay on raw data."""

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from weakref import ReferenceType, ref

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.dialect import database_operation
from app.models.heart_projection import HeartHour, HeartProjectionState

METHOD = "google-heart-original/1"
TABLES = {HeartHour.__tablename__, HeartProjectionState.__tablename__}
HOUR = timedelta(hours=1)
Sample = tuple[datetime, float, object]
_DEFAULT_FILTERS: dict[int, ReferenceType[ColumnElement[bool]]] = {}


def default_filter(predicate: ColumnElement[bool]) -> ColumnElement[bool]:
    """Recognize only the original default expression, never a transformed SQL clause.

    Structural comparison cannot traverse the eligibility query's ORM aliases. Weak
    identity registration avoids retaining queries and makes every clone/opaque caller
    conservatively fall back, even if SQLAlchemy copies expression annotations.
    """
    key = id(predicate)

    def discard(_reference: ReferenceType) -> None:
        _DEFAULT_FILTERS.pop(key, None)

    _DEFAULT_FILTERS[key] = ref(predicate, discard)
    return predicate


def hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def digest(samples: list) -> str:
    encoded = json.dumps(samples, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def unpack(row: HeartHour, start: datetime, stop: datetime) -> list[Sample] | None:
    if row.method != METHOD or row.built_revision != row.revision or row.samples is None:
        return None
    try:
        if digest(row.samples) != row.digest:
            return None
        found = []
        for raw_id, raw_time, parsed_time, bpm_hex, platform in row.samples:
            source_time = datetime.fromisoformat(raw_time)
            moment, bpm = datetime.fromisoformat(parsed_time), float.fromhex(bpm_hex)
            if (
                not isinstance(raw_id, str)
                or source_time.tzinfo is None
                or moment.tzinfo is None
                or hour(source_time) != row.bucket
                or not math.isfinite(bpm)
                or bpm < 0
            ):
                return None
            # Raw time, not the retained normalized timestamp or parsed sample time.
            if start <= source_time < stop:
                found.append((moment, bpm, platform))
        return found
    except (ValueError, TypeError, OverflowError):
        return None


@database_operation
async def packets(
    session: AsyncSession, start: datetime, stop: datetime, source_filter: ColumnElement[bool]
) -> dict[datetime, list[Sample]] | None:
    """One snapshot of packets; never simplify a caller's opaque SQL predicate.

    Only the exact default eligibility expression shares this read model. Deletion
    inclusion or additional clauses use the original reader without losing a condition.
    Builders never run in a request, including concurrent first readers.
    """
    registered = _DEFAULT_FILTERS.get(id(source_filter))
    if registered is None or registered() is not source_filter:
        return None
    ready = await session.scalar(
        select(HeartProjectionState.ready).where(HeartProjectionState.id == 1)
    )
    if not ready:
        return None
    rows = await session.scalars(
        select(HeartHour)
        .where(HeartHour.bucket >= hour(start), HeartHour.bucket < stop)
        .order_by(HeartHour.bucket)
        .execution_options(populate_existing=True)
    )
    found = {}
    for row in rows:
        values = unpack(row, start, stop)
        if values is not None:
            found[row.bucket] = values
    return found
