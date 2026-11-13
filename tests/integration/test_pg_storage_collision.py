"""A page that collides with a writer outside run ownership retries a bounded, jittered number
of times (``_Collision``, app/services/storage.py), then gives up without storing anything."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from identity_records import HEART, heart, raw_rows, store

from app.services.archive import storage

pytestmark = pytest.mark.integration


async def test_a_page_that_keeps_colliding_gives_up_after_bounded_jittered_waits(
    pg_sessions, monkeypatch
):
    await store(pg_sessions, HEART, heart(60))
    [before] = await raw_rows(pg_sessions, HEART)
    calls, waits, spreads = [], [], []
    original = storage._lock_rows

    async def collide(session, spec, google_ids, resolution):
        calls.append(1)
        await original(session, spec, google_ids, resolution)
        raise storage._Collision

    async def no_sleep(delay):
        waits.append(delay)

    def biggest(low, high):
        spreads.append((low, high))
        return high

    monkeypatch.setattr(storage, "_lock_rows", collide)
    monkeypatch.setattr(storage, "asyncio", SimpleNamespace(sleep=no_sleep))
    monkeypatch.setattr(storage, "random", SimpleNamespace(uniform=biggest))

    with pytest.raises(RuntimeError, match="a concurrent writer kept changing"):
        await store(pg_sessions, HEART, heart(61))

    assert len(calls) == storage.WRITE_ATTEMPTS
    caps = [0.05, 0.1, 0.2] + [0.4] * (storage.WRITE_ATTEMPTS - 4)
    assert spreads == [(0, cap) for cap in caps][: storage.WRITE_ATTEMPTS - 1]
    assert waits == caps[: storage.WRITE_ATTEMPTS - 1]  # the jittered value, bounded
    [after] = await raw_rows(pg_sessions, HEART)
    assert after.raw_json == before.raw_json == heart(60)  # nothing of the page was stored


async def test_a_page_retries_after_the_wait_and_stores_its_records_once(pg_sessions, monkeypatch):
    await store(pg_sessions, HEART, heart(60))
    calls = []
    waits = []
    original = storage._lock_rows

    async def collide_once(session, spec, google_ids, resolution):
        calls.append(1)
        if len(calls) == 1:
            raise storage._Collision
        return await original(session, spec, google_ids, resolution)

    async def no_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(storage, "_lock_rows", collide_once)
    monkeypatch.setattr(storage, "asyncio", SimpleNamespace(sleep=no_sleep))

    stats = await store(pg_sessions, HEART, heart(61))

    assert len(calls) == 2 and len(waits) == 1 and stats.updated == 1
    [row] = await raw_rows(pg_sessions, HEART)
    assert row.raw_json == heart(61)
