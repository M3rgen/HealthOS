"""Offline lifecycle checks; PostgreSQL ownership is verified separately."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.sync import ownership, watchdog


@pytest.fixture
def protocol(monkeypatch):
    engine = Mock(spec=watchdog.AsyncEngine)
    engine.dialect = SimpleNamespace(name="postgresql")
    connection = AsyncMock(spec=ownership.AsyncConnection)
    engine.connect = AsyncMock(return_value=connection)
    engine.get_execution_options.return_value = {"ownership_concurrency": 1}
    owner = watchdog.PipelineWatchdog(Mock(), SimpleNamespace(kw={"bind": engine}), Mock(), Mock())
    return SimpleNamespace(owner=owner, connection=connection, engine=engine)


@pytest.mark.parametrize("ending", ["success", "body-error", "busy"])
async def test_shared_pool_connection_is_discarded_on_every_exit(protocol, ending):
    p = protocol
    p.connection.scalar.return_value = ending != "busy"

    with pytest.raises(RuntimeError) if ending == "body-error" else nullcontext():
        async with p.owner._global_lock() as acquired:
            assert acquired == (ending != "busy")
            p.connection.begin.return_value.__aexit__.assert_awaited_once_with(None, None, None)
            if ending == "body-error":
                raise RuntimeError("synthetic body")

    p.connection.invalidate.assert_awaited_once()
    p.connection.close.assert_awaited_once()
    p.engine.dispose.assert_not_called()


@pytest.mark.parametrize("phase", ["invalidate", "close"])
async def test_repeated_cancellation_waits_for_cleanup(protocol, phase):
    closing, release = asyncio.Event(), asyncio.Event()

    async def pause():
        closing.set()
        await release.wait()

    getattr(protocol.connection, phase).side_effect = pause

    before = asyncio.all_tasks()
    task = asyncio.create_task(ownership.discard_connection(protocol.connection))
    try:
        await asyncio.wait_for(closing.wait(), 30)
        task.cancel()
        asyncio.get_running_loop().call_soon(task.cancel)
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)
    protocol.engine.dispose.assert_not_called()
    assert asyncio.all_tasks() == before
