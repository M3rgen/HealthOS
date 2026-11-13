"""Garmin run ownership on PostgreSQL: first rows, backfill bounds, a lost owner.

Two engines stand for two processes (the CLI backfill and the app's scheduled pull)."""

import asyncio
import collections
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest
import pytest_asyncio
from garmin_ownership_records import (
    assert_partial_normalization,
    at_boundaries,
    normalization_loss,
    raw_loss,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.waits import blocked_on_a_lock, until

from app.core.config import Settings
from app.core.ingestion_stats import StoreStats
from app.db.contracts import OwnershipLost
from app.db.postgres import advisory_key
from app.garmin.client import GarminError
from app.models.garmin import GarminPullState
from app.services.providers.garmin_ingestion import GarminPage
from app.services.providers.garmin_kinds import BY_NAME
from app.services.providers.garmin_normalize_types import NormalizeStats
from app.services.providers.garmin_pull import OWNERSHIP_LOST, GarminPull
from app.sync.ownership import GARMIN_NAMESPACE, garmin_ownership

pytestmark = pytest.mark.integration

SETTINGS = Settings(_env_file=None, garmin_enabled=True)
JANUARY = (date(2026, 1, 1), date(2026, 1, 31))
MARCH = (date(2026, 3, 1), date(2026, 3, 31))


class Source:
    """A Garmin source without the network: records each requested day."""

    name = "garmin"
    readers = None

    def __init__(self):
        self.days: list[date] = []
        self.fail_on: date | None = None
        self.hold: dict[date, asyncio.Event] = {}
        self.reached = collections.defaultdict(asyncio.Event)

    async def fetch(self, stream, window, *, on_request=None, **kwargs):
        day = window.start.date()
        if on_request:
            on_request()
        self.days.append(day)
        self.reached[day].set()
        if day in self.hold:
            await self.hold[day].wait()
        if day == self.fail_on:
            raise GarminError("synthetic failure")
        yield GarminPage([], None, [])

    async def store_raw(self, session, stream, page, **kwargs):
        return StoreStats()

    async def keep_after(self, page, failure):
        pass

    async def status(self, session):
        return {}

    async def normalize(self, window, *, sessions=None, seen_since=None):
        return NormalizeStats()


@pytest_asyncio.fixture
async def other_process(pg_sessions) -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(pg_sessions.kw["bind"].url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def pull(sessions, source=None):
    return GarminPull(SETTINGS, sessions, source=source or Source()).owned_by(
        garmin_ownership(sessions)
    )


async def state(sessions, kind):
    async with sessions() as session:
        return await session.get(GarminPullState, kind)


async def test_first_row_race_is_not_an_integrity_error(pg_sessions, other_process):
    pulls = [pull(pg_sessions), pull(other_process)]
    results = await asyncio.gather(
        *(
            pulls[i % 2].checkpoints.mark(pulls[i % 2].sessions, "steps", success=True)
            for i in range(8)
        ),
        return_exceptions=True,
    )
    assert [r for r in results if r is not None] == []
    async with pg_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GarminPullState)) == 1


async def test_backfills_of_different_bounds_never_interleave(pg_sessions, other_process):
    january_source, march_source = Source(), Source()
    release = asyncio.Event()
    january_source.hold[date(2026, 1, 5)] = release
    january, march = pull(pg_sessions, january_source), pull(other_process, march_source)

    first = asyncio.create_task(january.backfill(*JANUARY, kinds=["sleep"]))
    await until(lambda: january_source.reached[date(2026, 1, 5)].is_set(), "the fifth day")
    assert (await state(pg_sessions, "sleep")).backfill_next == date(2026, 1, 5)

    second = asyncio.create_task(march.backfill(*MARCH, kinds=["sleep"]))
    # The second run waits in PostgreSQL; it neither fetches nor touches the bounds.
    await blocked_on_a_lock(pg_sessions, second)
    assert march_source.days == []
    assert (await state(pg_sessions, "sleep")).backfill_since == JANUARY[0]

    release.set()
    reports = await asyncio.gather(first, second)

    assert not any(r["failed"] for r in reports)
    assert january_source.days == [JANUARY[0] + timedelta(days=d) for d in range(31)]
    assert march_source.days == [MARCH[0] + timedelta(days=d) for d in range(31)]
    finished = await state(pg_sessions, "sleep")
    assert (finished.backfill_since, finished.backfill_until) == MARCH
    assert finished.backfill_next is None


async def test_a_march_window_resumes_in_march(pg_sessions):
    source = Source()
    source.fail_on = date(2026, 3, 15)
    runner = pull(pg_sessions, source)

    report = await runner.backfill(*MARCH, kinds=["sleep"])
    assert report["failed"] and report["kinds"]["sleep"]["error"]
    assert (await state(pg_sessions, "sleep")).backfill_next == date(2026, 3, 15)

    source.fail_on = None
    source.days.clear()
    resumed = await runner.backfill(*MARCH, kinds=["sleep"])

    assert resumed["kinds"]["sleep"]["resumed_from"] == "2026-03-15"
    assert source.days[0] == date(2026, 3, 15)
    assert all(day.month == 3 for day in source.days)


async def test_a_stale_writer_cannot_move_another_runs_checkpoint(pg_sessions):
    runner = pull(pg_sessions)
    await runner.checkpoints.checkpoint(pg_sessions, "sleep", *MARCH)

    with pytest.raises(OwnershipLost, match="another run"):
        await runner.checkpoints.advance(pg_sessions, "sleep", *JANUARY, date(2026, 1, 20))

    row = await state(pg_sessions, "sleep")
    assert (row.backfill_since, row.backfill_next) == (MARCH[0], MARCH[0])


async def test_a_lost_owner_stops_and_the_next_run_resumes(pg_sessions, other_process):
    source = Source()
    release = asyncio.Event()
    source.hold[date(2026, 3, 3)] = release
    runner = pull(pg_sessions, source)

    task = asyncio.create_task(runner.backfill(*MARCH, kinds=["sleep"]))
    await until(lambda: source.reached[date(2026, 3, 3)].is_set(), "the third day")
    assert (await state(pg_sessions, "sleep")).backfill_next == date(2026, 3, 3)
    async with other_process.begin() as session:
        killed = await session.scalar(
            text(
                # pg_locks spans the server: without the database, a parallel worker's owner
                # (its own test database, same key) would be terminated too.
                "SELECT pg_terminate_backend(l.pid, 10000) FROM pg_locks l "
                "JOIN pg_stat_activity a ON a.pid = l.pid AND a.datname = current_database() "
                "WHERE l.locktype = 'advisory' AND l.classid = :namespace AND l.granted"
            ),
            {"namespace": GARMIN_NAMESPACE},
        )
    assert killed
    release.set()
    report = await task

    assert report["kinds"]["sleep"]["error"] == OWNERSHIP_LOST
    assert report["failed"] and not report["kinds"]["sleep"]["complete"]
    # The checkpoint stays where the lost owner left it; the request made after the loss is
    # not recorded, so the next run asks for that day again.
    row = await state(pg_sessions, "sleep")
    assert (row.backfill_since, row.backfill_next) == (MARCH[0], date(2026, 3, 3))
    assert source.days[-1] == date(2026, 3, 3)

    fresh = Source()
    report = await pull(other_process, fresh).backfill(*MARCH, kinds=["sleep"])
    assert report["kinds"]["sleep"]["resumed_from"] == "2026-03-03"
    assert fresh.days[0] == date(2026, 3, 3) and len(fresh.days) == 29


async def test_one_run_of_a_kind_at_a_time_across_a_scheduled_pull(pg_sessions, other_process):
    backfill_source, scheduled_source = Source(), Source()
    release = asyncio.Event()
    backfill_source.hold[date(2026, 3, 2)] = release

    backfill = asyncio.create_task(
        pull(pg_sessions, backfill_source).backfill(*MARCH, kinds=["sleep"])
    )
    await until(lambda: backfill_source.reached[date(2026, 3, 2)].is_set(), "the second day")
    scheduled = asyncio.create_task(pull(other_process, scheduled_source).run(["sleep"]))
    await blocked_on_a_lock(pg_sessions, scheduled)
    assert scheduled_source.days == []

    release.set()
    await asyncio.gather(backfill, scheduled)
    assert scheduled_source.days, "the pull ran once the backfill released the kind"


def test_sleep_is_a_day_kind():
    # The tests above rely on one request per day (params["date"]).
    assert BY_NAME["sleep"].mode == "day" and BY_NAME["sleep"].max_days == 1


@pytest.fixture
def connection_loss(pg_sessions, other_process):
    owner = garmin_ownership(pg_sessions)

    async def lose(key):
        async with other_process.begin() as session:
            # The owned connection must be idle while waiting for HTTP or the next chunk.
            state = await session.execute(
                text(
                    "SELECT a.state, pg_terminate_backend(a.pid, 10000) "
                    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "AND a.datname = current_database() "
                    "WHERE l.locktype = 'advisory' AND l.classid = :namespace "
                    "AND l.objid = :key AND l.granted"
                ),
                {"namespace": GARMIN_NAMESPACE, "key": advisory_key(key) % 2**32},
            )
            assert state.one() == ("idle", True)

    return owner, lose


@pytest.mark.parametrize("lane", ["pull", "backfill"])
@pytest.mark.parametrize("kind", ["steps", "activities"])
async def test_real_connection_loss_fences_raw_pages(
    pg_sessions, tmp_path, monkeypatch, connection_loss, lane, kind
):
    owner, lose = connection_loss
    await raw_loss(pg_sessions, tmp_path, monkeypatch, lane, kind, owner, lose)


@pytest.mark.parametrize("lane", ["pull", "backfill", "normalize"])
@pytest.mark.parametrize("kind", ["steps", "activities"])
async def test_real_connection_loss_between_normalization_commits(
    pg_sessions, tmp_path, monkeypatch, connection_loss, lane, kind
):
    owner, lose = connection_loss
    runner, boundary, invoke, before, model, result = await normalization_loss(
        pg_sessions, tmp_path, monkeypatch, lane, kind, owner, lose
    )
    runner.owned_by(at_boundaries(owner, boundary))
    try:
        await assert_partial_normalization(pg_sessions, runner, invoke, before, model, result)
    finally:
        await runner.aclose()
