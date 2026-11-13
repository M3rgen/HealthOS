"""Backfill starts join the run in progress instead of stacking a new one per POST."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from app.api import backfill
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.services.synchronization.sync_jobs import BackfillJobs

HEADERS = {"X-HealthOS-Request": "1"}


class Backfill:
    """A run that holds until released; records what it was asked to do."""

    def __init__(self) -> None:
        self.specs = {"steps": object(), "sleep": object()}
        self.calls: list[list[str] | None] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def run(self, data_types=None) -> int:
        self.calls.append(data_types)
        self.started.set()
        await self.release.wait()
        return 0


@pytest.fixture
async def api():
    app = FastAPI()
    settings = Settings(_env_file=None, manual_write_requests_per_minute=1000)
    app.state.settings = settings
    app.state.write_limits = WriteLimits(settings)
    tasks: set[asyncio.Task] = set()

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.add(task)
        return task

    runner = Backfill()
    app.state.runtime = SimpleNamespace(
        backfill=runner, backfill_jobs=BackfillJobs(), spawn=Mock(side_effect=spawn)
    )
    app.include_router(backfill.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as http:
        yield http, app.state.runtime, runner
    runner.release.set()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "path,key,selected,data_types",
    [
        ("/backfill/start", None, None, ["steps", "sleep"]),
        ("/backfill/start?data_type=steps", "steps", ["steps"], ["steps"]),
    ],
)
async def test_twenty_posts_start_one_run_and_answer_as_before(
    api, path, key, selected, data_types
):
    http, runtime, runner = api
    responses = await asyncio.gather(*(http.post(path, headers=HEADERS) for _ in range(20)))
    await asyncio.wait_for(runner.started.wait(), 30)
    assert [response.status_code for response in responses] == [202] * 20
    assert all(
        response.json() == {"status": "accepted", "data_types": data_types}
        for response in responses
    )
    assert runner.calls == [selected]
    assert runtime.spawn.call_count == 1
    assert list(runtime.backfill_jobs.active) == [key]


async def test_a_running_job_is_left_alone_and_a_later_post_starts_a_new_one(api):
    http, runtime, runner = api
    await http.post("/backfill/start?data_type=steps", headers=HEADERS)
    await asyncio.wait_for(runner.started.wait(), 30)
    job = runtime.backfill_jobs.active["steps"]
    await http.post("/backfill/start?data_type=steps", headers=HEADERS)
    assert not job.task.done() and not job.task.cancelled()
    assert runtime.backfill_jobs.active["steps"] is job
    runner.release.set()
    await asyncio.wait_for(job.task, 30)
    await asyncio.sleep(0)  # the task's done callbacks run on the next loop iteration
    assert runtime.backfill_jobs.active == {}
    runner.release = asyncio.Event()
    await http.post("/backfill/start?data_type=steps", headers=HEADERS)
    assert runtime.spawn.call_count == 2
    assert len(runner.calls) >= 1
    assert runtime.backfill_jobs.active["steps"] is not job


async def test_other_selections_start_their_own_run(api):
    http, runtime, runner = api
    for path in (
        "/backfill/start?data_type=steps",
        "/backfill/start",
        "/backfill/start?data_type=sleep",
    ):
        await http.post(path, headers=HEADERS)
    await asyncio.sleep(0)
    assert runtime.spawn.call_count == 3
    assert set(runtime.backfill_jobs.active) == {"steps", None, "sleep"}


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
async def test_a_failed_or_cancelled_run_does_not_block_the_next(ending):
    release = asyncio.Event()

    async def run(data_types=None):
        await release.wait()
        if ending == "failed":
            raise RuntimeError("synthetic failure")

    jobs = BackfillJobs()
    runner = SimpleNamespace(run=run)
    job = jobs.start("steps", runner, asyncio.create_task)
    if ending == "cancelled":
        job.task.cancel()
    release.set()
    await asyncio.gather(job.task, return_exceptions=True)
    await asyncio.sleep(0)
    assert jobs.active == {}
    next_job = jobs.start("steps", runner, asyncio.create_task)
    assert next_job is not job
    await asyncio.gather(next_job.task, return_exceptions=True)
