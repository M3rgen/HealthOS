"""The APScheduler adapter drains the pinned executor's real coroutine futures."""

import asyncio

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from app.core.config import Settings
from app.runtime import build_runtime
from app.services.synchronization.scheduler_shutdown import AppScheduler


async def test_runtime_tracks_scheduler_state_and_waits_for_every_job_cleanup(sessions):
    runtime = build_runtime(Settings(_env_file=None), sessions)
    scheduler = AppScheduler()
    started = [asyncio.Event(), asyncio.Event()]
    cleaning = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    cleaned = []

    async def job(index):
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning[index].set()
            await release.wait()
            cleaned.append(index)

    shutdown = None
    try:
        assert not runtime.scheduler_running
        runtime.scheduler = scheduler
        assert not runtime.scheduler_running
        for index in range(2):
            scheduler.add_job(job, args=[index])
        scheduler.start()
        assert runtime.scheduler_running
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 10)
        shutdown = asyncio.create_task(scheduler.shutdown_and_wait())
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in cleaning)), 10)
        assert not shutdown.done() and not runtime.scheduler_running
        assert len(scheduler.draining_executor.cancelled_jobs) == 2
        release.set()
        await asyncio.wait_for(shutdown, 10)
        assert sorted(cleaned) == [0, 1]
        assert scheduler.draining_executor.cancelled_jobs == []
        runtime.scheduler = None
        assert not runtime.scheduler_running
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        elif scheduler.running:
            await scheduler.shutdown_and_wait()
        await runtime.shutdown()


@pytest.mark.parametrize("failed", [False, True])
async def test_finished_jobs_leave_no_futures_to_drain(failed):
    scheduler = AppScheduler()
    finished = asyncio.Event()

    async def job():
        if failed:
            raise ValueError("synthetic")

    scheduler.add_listener(lambda event: finished.set(), EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
    scheduler.add_job(job)
    scheduler.start()
    try:
        await asyncio.wait_for(finished.wait(), 10)
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.wait_for(scheduler.draining_executor.stopped.wait(), 10)
    assert scheduler.draining_executor.cancelled_jobs == []
