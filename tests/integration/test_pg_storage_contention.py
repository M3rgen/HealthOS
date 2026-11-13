"""Pages outside run ownership must finish despite overlapping, reversed key order."""

import asyncio
from datetime import timedelta

import pytest
from identity_records import HEART, T0, heart
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.health import HeartRate
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.archive.storage import store_records

pytestmark = pytest.mark.integration


async def test_32_writers_store_60_overlapping_records_for_three_rounds(database_url, migrated):
    # Every task holds a separate connection before the round starts: the default pool
    # would otherwise throttle the 32 writers and conceal contention.
    engine = create_async_engine(database_url, pool_size=32, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    measurements = []
    try:
        for turn in range(3):
            ready = asyncio.Barrier(32)

            async def write(writer, turn=turn, ready=ready):
                records = [
                    heart(60 + turn * 32 + writer, T0 + timedelta(seconds=key)) for key in range(60)
                ]
                async with sessions.begin() as session:
                    await session.connection()
                    await ready.wait()
                    return await store_records(
                        session, HEART, records if writer % 2 else records[::-1]
                    )

            results = await asyncio.gather(*(write(i) for i in range(32)), return_exceptions=True)
            failures = [result for result in results if isinstance(result, BaseException)]
            abandoned = sum(
                isinstance(error, RuntimeError) and "kept changing the rows" in str(error)
                for error in failures
            )
            async with sessions() as session:
                raw = await session.scalar(select(func.count()).select_from(RawHealthData))
                normalized = await session.scalar(select(func.count()).select_from(HeartRate))
                versions = await session.scalar(
                    select(func.count()).select_from(RawHealthDataVersion)
                )
            print(f"round={turn + 1}: abandoned={abandoned}/32, rows={raw}/60")
            measurements.append((len(failures), raw, normalized, versions))
            assert all(isinstance(error, RuntimeError) for error in failures), failures
        assert measurements == [(0, 60, 60, (32 * turn - 1) * 60) for turn in range(1, 4)]
    finally:
        await engine.dispose()
