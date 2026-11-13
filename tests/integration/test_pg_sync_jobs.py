"""Real PostgreSQL pool counters for the twenty-request admission acceptance check."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_sync_jobs import assert_concurrent_sync

from app.core.config import Settings
from app.db.engine import create_shared_engine

pytestmark = pytest.mark.integration


async def test_twenty_sync_requests_share_job_and_postgres_pool(database_url, migrated):
    engine = create_shared_engine(
        Settings(_env_file=None, database_url=database_url, sync_concurrency=2)
    )
    try:
        await assert_concurrent_sync(
            async_sessionmaker(engine, expire_on_commit=False), "/sync/all"
        )
        assert engine.pool.size() == 9 and engine.pool._max_overflow == 0
    finally:
        await engine.dispose()
