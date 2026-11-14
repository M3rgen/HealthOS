"""Bounded source JSON reading for coverage types without a metric adapter."""

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.google_health.data_types import DataTypeSpec, RecordKind

from .coverage_reading import Stream, stream_of
from .coverage_types import Window

# Bound transient source JSON, independently of the record and response budgets.
# asyncpg fetches at least 50 rows even when yield_per requests fewer.
_SOURCE_BATCH = 64


async def source_streams(
    session: AsyncSession, statement: Select, spec: DataTypeSpec, window: Window
) -> dict[str, Stream]:
    """Consume only source JSON and its timestamp; retain source/day summaries.

    The caller supplies the same type, date and raw_counted predicates used for
    presence. JSON stays JSON (including boolean sources), so grouping retains
    the stored source's fingerprint. Only the cursor belongs to this reader;
    transaction and session lifetime remain with the caller.
    """
    streams: dict[str, Stream] = {}
    rows = await session.stream(statement, execution_options={"yield_per": _SOURCE_BATCH})
    try:
        async for data_source, stamp in rows:
            day = as_utc(stamp).date() if spec.kind == RecordKind.daily else window.local(stamp)
            if window.first <= day <= window.end:
                stream = stream_of(data_source, streams)
                stream.records += 1
                stream.days.add(day)
    finally:
        # Release the cursor on grouping/fetch failure and actual task cancellation.
        await rows.close()
    return streams
