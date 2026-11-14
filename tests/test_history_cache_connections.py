"""Independent process caches use database state, not shared Python invalidation hooks."""

import pytest
from derived_records import raw_mutation, store
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_history_bounds_cache import MIDDLE, checked, payload, read

from app.db.base import Base
from app.services.dashboard_views import history_bounds


@pytest.mark.parametrize("operation", ["supersession", "device", "move", "zone"])
async def test_identity_and_bulk_maintenance_writers(sessions, monkeypatch, operation):
    await store(sessions, "steps", payload("steps", "middle", MIDDLE))
    mutation = await raw_mutation(sessions, "steps", operation)
    await checked(sessions, monkeypatch, "steps", MIDDLE, MIDDLE)
    async with sessions.begin() as session:
        await session.execute(mutation)
    await checked(sessions, monkeypatch, "steps", MIDDLE, MIDDLE)


async def test_independent_engines_and_caches_observe_commits(sessions, monkeypatch, tmp_path):
    original = sessions.kw["bind"]
    url = original.url
    if url.get_backend_name() == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'shared.db'}"
    left = create_async_engine(url)
    right = create_async_engine(url)
    try:
        if left.dialect.name == "sqlite":
            async with left.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        factories = [async_sessionmaker(engine, expire_on_commit=False) for engine in (left, right)]
        # Each instance models an independent process. Neither receives a callback
        # when another connection commits, rolls back, or publishes a correction.
        caches = [history_bounds.BoundsCache(), history_bounds.BoundsCache()]
        for factory, cache in zip(factories, caches, strict=True):
            monkeypatch.setattr(history_bounds, "_CACHE", cache)
            await checked(factory, monkeypatch, "steps", None, None)
        await store(factories[1], "steps", payload("steps", "middle", MIDDLE))
        for factory, cache in zip(factories, caches, strict=True):
            monkeypatch.setattr(history_bounds, "_CACHE", cache)
            await checked(factory, monkeypatch, "steps", MIDDLE, MIDDLE)
        await store(factories[1], "steps", payload("steps", "middle", MIDDLE, True))
        for factory, cache in zip(factories, caches, strict=True):
            monkeypatch.setattr(history_bounds, "_CACHE", cache)
            await checked(factory, monkeypatch, "steps", None, None)
        assert (await read(factories[0]))["earliest_date"] is None
    finally:
        await left.dispose()
        await right.dispose()
