"""Read daily sleep against published source signs before SQL/float rounding."""

import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, String, case, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.providers import GARMIN
from app.core.timeutil import as_utc
from app.models.garmin import GarminRawRecord
from app.models.health import SleepSession
from app.parsers.common import number
from app.services.interpretation.sleep_estimate import SleepAccumulator

from .garmin_daily_input import daily_input_rows
from .garmin_versions import current_row

_SLEEP_BATCH = 1000


def _negative(source: object) -> bool:
    # Decode just a selected JSON member. Default JSON floats can underflow a
    # numeric token before the sign check; Decimal also preserves numeric text.
    value = number(
        json.loads(source, parse_float=Decimal, parse_int=Decimal)
        if isinstance(source, str)
        else None
    )
    return value is not None and value.is_finite() and value < 0


def _query(start: datetime, stop: datetime, provider: str):
    row = SleepSession
    eligible = select(row).where(
        row.provider == provider,
        current_row(row),
        row.is_quarantined.is_(False),
        row.end_time >= start,
        row.end_time < stop,
    )
    resolved = daily_input_rows(eligible, row, "sleep", ("sleep", "sleep-summaries"))
    row = aliased(SleepSession, resolved)
    query = select(row).select_from(resolved)
    sources = []
    for index, kind in enumerate(("sleep", "sleep-summaries")):
        raw = aliased(GarminRawRecord, name=kind.replace("-", "_"))
        input_id = resolved.c[f"input_{index}"]
        query = query.outerjoin(raw, (row.provider == GARMIN) & (raw.id == input_id))
        sources.append(raw)
    primary, alternate = sources
    # The converter falls back by whole record, never by individual member.
    present = (
        primary.id.is_not(None)
        & (primary.body != {})
        & (primary.body != [])
        & (primary.body != JSON.NULL)
    )
    for member in ("sleepTimeSeconds", "awakeSleepSeconds"):
        query = query.add_columns(
            case(
                (present, sql_cast(primary.body.op("->")(member), String)),
                else_=sql_cast(alternate.body.op("->")(member), String),
            )
        )
    return query.order_by(row.end_time, row.id)


async def iter_sleep_days(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo, provider: str = GARMIN
) -> AsyncGenerator[tuple[date, float], None]:
    """One query, only two raw members, no raw bodies or per-night queries.

    Completed projections own immutable inputs; only pre-projection archives use
    current observation/legacy selection. Normalized-only and DEMO rows retain
    their existing calculation. Garmin bodies never enter Google's sleep parser.
    A negative selected source invalidates the whole wake day, even alongside a
    valid session. Keep the shared estimator's normalized negative checks too.

    Rows are ordered by UTC end time, then id, across all driver batches. Local
    dates need not be monotone (an offset rollback can cross midnight). Every
    Python UTC offset is strictly within +/-24 hours: once UTC reaches day D,
    no future row can wake before D-1. Finalize only days strictly before D-1.
    Thus at most three civil days retain events, even across date-line changes;
    empty calendar days require neither iteration nor queries. Ordinals avoid
    subtracting past datetime.min. The caller must close this iterator on exit.
    """
    days: dict[date, SleepAccumulator] = {}
    invalid: set[date] = set()
    rows = await session.stream(
        _query(start, stop, provider).execution_options(yield_per=_SLEEP_BATCH)
    )
    try:
        async for row, total, awake in rows.tuples():
            end = as_utc(row.end_time)
            for day in sorted(days):
                if day.toordinal() >= end.date().toordinal() - 1:
                    break
                seconds = days.pop(day).result().seconds
                bad = day in invalid
                invalid.discard(day)
                if not bad and seconds is not None:
                    yield day, seconds
            day = end.astimezone(zone).date()
            if provider == GARMIN and (_negative(total) or _negative(awake)):
                invalid.add(day)
            accumulator = days.get(day)
            if accumulator is None:
                accumulator = days[day] = SleepAccumulator()
            accumulator.add(row, None)
            del accumulator  # a yielded result must not keep a completed day's events alive
    finally:
        await rows.close()
    for day in sorted(days):
        seconds = days.pop(day).result().seconds
        if day not in invalid and seconds is not None:
            yield day, seconds


async def sleep_days(
    session: AsyncSession, start: datetime, stop: datetime, zone: ZoneInfo, provider: str = GARMIN
) -> dict[date, float]:
    """Collect only daily values; completed days' session events are released."""
    async with aclosing(iter_sleep_days(session, start, stop, zone, provider)) as days:
        return {day: seconds async for day, seconds in days}
