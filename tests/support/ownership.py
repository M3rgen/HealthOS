"""Ownership test helpers: independent SQLite engines, contention, runners and workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from retrieval_records import END, START, point
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sync_ownership_records import service

from app.core.config import Settings
from app.db.base import Base
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PageBudget
from app.services.providers.ingestion import GoogleSource
from app.sync import backfill, ownership
from app.sync.backfill import BackfillService


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def independent(sessions):
    engine = create_async_engine(sessions.kw["bind"].url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def contention(monkeypatch):
    blocked, retry = asyncio.Event(), asyncio.Event()

    async def pause():
        # Hold only the first waiter; later contenders queue behind it as in production.
        if not blocked.is_set():
            blocked.set()
            await retry.wait()

    monkeypatch.setattr(ownership, "_contended", pause)
    return blocked, retry


def runner(sessions, client):
    return backfill.BackfillService(
        Settings(_env_file=None, backfill_start_date=START.date(), backfill_window_days=1),
        sessions,
        GoogleSource(client),
        dict(REGISTRY),
    )


@pytest.fixture(autouse=True)
def horizon(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return END

    monkeypatch.setattr(backfill, "datetime", Frozen)


def worker(url, pipe, kind, start, job_id=None):
    async def run():
        # Match the shared pool's minimum capacity: notification + data type +
        # watchdog ownership, with two ordinary slots for OAuth and response archiving.
        engine = create_async_engine(
            url,
            pool_size=5,
            max_overflow=0,
            pool_timeout=30,
            connect_args={"server_settings": {"TimeZone": "America/Los_Angeles"}},
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        class Client:
            async def list_data_points(self, spec, lower, upper, page_token=None):
                async with sessions() as session:
                    assert await session.scalar(text("SELECT 1")) == 1
                pipe.send(("fetch", lower, upper, page_token))
                action = await asyncio.to_thread(pipe.recv)
                return Page([point(kind=kind)], "p2") if action == "more" else Page([], None)

            async def keep_after(self, responses, failure):
                pass

        async def pause():
            pipe.send(("busy",))
            assert await asyncio.to_thread(pipe.recv) == "retry"

        ownership._contended = pause
        try:
            if job_id is None:
                await service(sessions, Client()).run(kind, start, start + timedelta(days=1))
            else:
                await BackfillService(
                    Settings(_env_file=None), sessions, GoogleSource(Client()), dict(REGISTRY)
                )._run_next_window(kind, PageBudget())
            pipe.send(("result", "completed"))
        except Exception as exc:
            pipe.send(("result", type(exc).__name__))
        finally:
            await engine.dispose()
            pipe.close()

    asyncio.run(run())


async def receive(pipe):
    def read():
        assert pipe.poll(20), "worker failed to reach the next controlled event"
        return pipe.recv()

    return await asyncio.to_thread(read)
