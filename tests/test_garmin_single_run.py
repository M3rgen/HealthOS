"""One Garmin run at a time, whatever concurrency the engine allows Google runs."""

from __future__ import annotations

import asyncio

import pytest
from support.waits import until

from app.sync import ownership
from app.sync.ownership import GARMIN_NAMESPACE, RUN_NAMESPACE, run_ownership


def queued(engine, namespace: int) -> int:
    waiters = ownership._quota(engine, namespace).owners._waiters or ()
    return sum(not waiter.cancelled() for waiter in waiters)


async def _own(sessions, key, namespace, entered, release):
    async with run_ownership(sessions, key, namespace=namespace) as owned:
        assert owned is not None
        entered.set()
        await release.wait()


@pytest.mark.parametrize("configured", [2, 4])
async def test_a_second_garmin_kind_waits_though_the_engine_allows_more(sessions, configured):
    """Production sets ``ownership_concurrency`` from settings; Garmin must not follow it."""
    engine = sessions.kw["bind"]
    engine.update_execution_options(ownership_concurrency=configured)
    first_in, second_in, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(_own(sessions, "sleep", GARMIN_NAMESPACE, first_in, release))
    await asyncio.wait_for(first_in.wait(), 30)
    second = asyncio.create_task(_own(sessions, "steps", GARMIN_NAMESPACE, second_in, release))
    await until(
        lambda: second_in.is_set() or queued(engine, GARMIN_NAMESPACE) == 1,
        "the second Garmin kind to enter or to queue for the slot",
    )
    assert not second_in.is_set()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 30)
    assert second_in.is_set()


async def test_google_runs_of_two_data_types_follow_the_configured_limit(sessions):
    """The contrast that makes the test above about Garmin rather than the limit itself."""
    engine = sessions.kw["bind"]
    engine.update_execution_options(ownership_concurrency=2)
    first_in, second_in, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(_own(sessions, "sleep", RUN_NAMESPACE, first_in, release))
    second = asyncio.create_task(_own(sessions, "steps", RUN_NAMESPACE, second_in, release))
    await asyncio.wait_for(asyncio.gather(first_in.wait(), second_in.wait()), 30)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 30)
