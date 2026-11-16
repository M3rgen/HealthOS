"""The application's pool opens its connections with JIT off; other connections keep the server's.

A reader statement's per-row eligibility subqueries push its cost estimate past the JIT
thresholds while it reads a few thousand rows; compiling it took longer than running it.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.engine import create_shared_engine

pytestmark = pytest.mark.integration


async def test_pool_connections_run_without_jit(database_url):
    shared = create_shared_engine(Settings(_env_file=None, database_url=database_url))
    plain = create_async_engine(database_url)
    try:
        async with plain.connect() as connection:
            assert await connection.scalar(text("SHOW jit")) == "on"
        for _ in range(2):  # a returned and reused connection keeps the setting
            async with shared.connect() as connection:
                assert await connection.scalar(text("SHOW jit")) == "off"
                await connection.rollback()
    finally:
        await shared.dispose()
        await plain.dispose()
