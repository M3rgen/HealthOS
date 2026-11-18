"""Archive-independent hot history reads; large runs are opt-in benchmark measurements."""

import os

import pytest
from history_cache_archive import measured_page, seed_minutes, stable_history_snapshot
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base


@pytest.mark.parametrize(
    "days", [365, 1825] if os.getenv("HEALTHOS_HISTORY_BENCHMARK") else [8, 40]
)
@pytest.mark.timeout(580 if os.getenv("HEALTHOS_HISTORY_BENCHMARK") else 60)
async def test_history_reads_scale(sessions, monkeypatch, tmp_path, days):
    engine = None
    large = os.getenv("HEALTHOS_HISTORY_BENCHMARK")
    if large and sessions.kw["bind"].dialect.name == "sqlite":
        # The 1.8M-row fixture exceeds the memory-only SQLite database's capacity.
        # Keep the benchmark under the same 2 GiB process limit as the unit suite.
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await seed_minutes(sessions, days, 1000 if large else 4)
        async with stable_history_snapshot(sessions) as readers:
            before, old_first = await measured_page(readers, monkeypatch, use_bounds_cache=False)
            _, old_second = await measured_page(readers, monkeypatch, use_bounds_cache=False)
            first, cold = await measured_page(readers, monkeypatch)
            second, hot = await measured_page(readers, monkeypatch)
        print(f"history days={days}: before={old_first}/{old_second}, after={cold}/{hot}")
        assert before == first == second
        per_day = 1000 if large else 4
        # Bounds read the first and newest start (one buffered row), the oldest four days
        # and the rows ending in the newest five (four-day window, its edge day included):
        # the same rows for every archive length.
        streamed = (4 + 5) * per_day
        # GitHub-only Apple extension: one more bounds aggregate (one row) and one more page read.
        assert old_first == old_second == (7 + 2, streamed + 1 + 1, streamed)
        assert cold == (9 + 2, streamed + 3 + 1, streamed)
        assert hot == (4 + 1, 1, 0)
    finally:
        if engine is not None:
            await engine.dispose()
