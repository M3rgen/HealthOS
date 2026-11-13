"""On one SQLite connection two data types' setup never interleaves; on PostgreSQL it may."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.sync import backfill


class Overlap:
    def __init__(self) -> None:
        self.now = 0
        self.most = 0

    @asynccontextmanager
    async def held(self):
        self.now += 1
        self.most = max(self.most, self.now)
        try:
            await asyncio.sleep(0)
            yield self
        finally:
            self.now -= 1

    async def scalar(self, _statement):
        await asyncio.sleep(0)


@pytest.mark.parametrize(("dialect", "most"), [("sqlite", 1), ("postgresql", 2)])
async def test_job_setup_and_selection_overlap_only_with_many_connections(
    monkeypatch, dialect, most
):
    setup, selection = Overlap(), Overlap()
    owned = SimpleNamespace(begin=selection.held)

    @asynccontextmanager
    async def run_ownership(_sessions, _data_type):
        await asyncio.sleep(0)
        yield owned

    async def ensure_jobs(*_arguments):
        async with setup.held():
            await asyncio.sleep(0)

    monkeypatch.setattr(backfill, "run_ownership", run_ownership)
    service = backfill.BackfillService(
        Settings(_env_file=None, sync_concurrency=2),
        SimpleNamespace(kw={"bind": SimpleNamespace(dialect=SimpleNamespace(name=dialect))}),
        SimpleNamespace(),
        dict.fromkeys(("steps", "sleep"), SimpleNamespace()),
    )
    monkeypatch.setattr(service, "_ensure_jobs", ensure_jobs)
    assert await service.run() == 0
    assert (setup.most, selection.most) == (most, most)
