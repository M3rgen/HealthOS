"""On a backend without savepoints (SQLite) a page has one attempt: its writer is alone."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.google_health.data_types import REGISTRY
from app.services.archive import storage
from app.services.archive.storage import store_records


async def test_a_collision_on_sqlite_is_not_retried_inside_one_transaction(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'single.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    calls = []

    async def collide(*_):
        calls.append(1)
        raise storage._Collision

    monkeypatch.setattr(storage, "_lock_rows", collide)
    record = {
        "name": "users/1/dataTypes/heart-rate/dataPoints/synthetic",
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z"},
            "beatsPerMinute": 60,
        },
    }
    try:
        with pytest.raises(RuntimeError, match="a concurrent writer kept changing"):
            async with sessions.begin() as session:
                await store_records(session, REGISTRY["heart-rate"], [record])
    finally:
        await engine.dispose()
    assert len(calls) == 1
