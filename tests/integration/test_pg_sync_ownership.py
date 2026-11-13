"""Real independent processes, PostgreSQL session loss, and durable checkpoint ownership."""

import asyncio
import multiprocessing
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from retrieval_records import END, START
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.ownership import receive, worker
from support.waits import until
from sync_ownership_records import service, snapshot

from app.sync import ownership
from app.sync.incremental import STALE_RUNNING_AFTER

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ending", ["completed", "terminated", "disconnected"])
async def test_process_ownership_and_restart(pg_sessions, ending):
    url = pg_sessions.kw["bind"].url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    children = []

    def start(kind, lower):
        parent, child = context.Pipe()
        process = context.Process(target=worker, args=(url, child, kind, lower))
        process.start()
        child.close()
        children.append((process, parent))
        return process, parent

    try:
        first, owner = start("heart-rate", START)
        assert await receive(owner) == ("fetch", START, END, None)
        owner.send("more")
        assert await receive(owner) == ("fetch", START, END, "p2")
        before = await snapshot(pg_sessions)
        second, waiter = start("heart-rate", END)
        assert await receive(waiter) == ("busy",)
        assert await snapshot(pg_sessions) == before
        # An aged heartbeat does not override a live PostgreSQL owner.
        recovery = service(pg_sessions, None)
        future = datetime.now(UTC) + STALE_RUNNING_AFTER + timedelta(minutes=1)
        assert await recovery.recover_interrupted(now=future) == 0
        assert await snapshot(pg_sessions) == before

        _, other = start("steps", START)
        assert await receive(other) == ("fetch", START, END, None)
        other.send("done")
        assert await receive(other) == ("result", "completed")
        async with pg_sessions() as session:
            pid, state = (
                await session.execute(
                    text(
                        "SELECT l.pid, a.state FROM pg_locks l "
                        "JOIN pg_stat_activity a ON a.pid = l.pid "
                        "AND a.datname = current_database() "
                        "WHERE l.locktype = 'advisory' AND l.classid = :namespace "
                        "AND l.objid = :key AND l.objsubid = 2 AND l.granted"
                    ),
                    {
                        "namespace": ownership.RUN_NAMESPACE,
                        "key": (zlib.crc32(b"heart-rate") - 2**31) % 2**32,
                    },
                )
            ).one()
        # Google I/O never holds a transaction open (state, not xact_start: see
        # test_pg_backfill_ownership).
        assert state == "idle"
        if ending == "terminated":
            first.terminate()
            await asyncio.to_thread(first.join, 10)
            assert not first.is_alive()
        if ending != "completed":
            # PostgreSQL acknowledges backend exit before the old client can continue.
            async with pg_sessions.begin() as session:
                await session.execute(
                    text(
                        "SELECT pg_terminate_backend(:pid, 10000) "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
        if ending != "terminated":
            owner.send("done")
            result = await receive(owner)
            assert result[0] == "result"
            assert (result[1] == "completed") == (ending == "completed")
        if ending != "completed":
            # No later write from the dead/disconnected owner changed its checkpoint.
            failed = await snapshot(pg_sessions)
            assert [
                row for row in failed["retrieval_checkpoints"] if row["owner"] == "heart-rate"
            ] == before["retrieval_checkpoints"]
            heart = next(row for row in failed["sync_state"] if row["data_type"] == "heart-rate")
            assert heart["last_page_token"] == "p2"
            assert await recovery.recover_interrupted(now=future) == 1
        waiter.send("retry")
        if ending != "completed":
            assert await receive(waiter) == ("fetch", START, END, "p2")
            waiter.send("done")
        assert await receive(waiter) == ("fetch", END, END + timedelta(days=1), None)
        waiter.send("done")
        assert await receive(waiter) == ("result", "completed")
        await asyncio.to_thread(second.join, 10)
        assert second.exitcode == 0
        final = await snapshot(pg_sessions)
        heart = next(row for row in final["sync_state"] if row["data_type"] == "heart-rate")
        assert heart["last_requested_from"] == END and heart["last_page_token"] is None
        for table in ("retrieval_receipts", "retrieval_events", "raw_health_data_versions"):
            assert all(row in final[table] for row in before[table])
        resumed = [row for row in final["retrieval_receipts"] if row["parent_event_id"] is not None]
        assert [row["parent_event_id"] for row in resumed] == (
            [] if ending == "completed" else [before["retrieval_checkpoints"][0]["event_id"]]
        )
    finally:
        for process, pipe in children:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 10)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 10)
            pipe.close()


async def waiting_backends(sessions):
    async with sessions() as session:
        rows = await session.execute(
            text(
                "SELECT pid, wait_event_type FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND query LIKE 'SELECT pg_advisory_lock(%'"
            )
        )
        return [tuple(row) for row in rows]


@pytest.mark.parametrize("slice_timeout", ["100ms", "2s"])
async def test_waiter_queues_on_one_backend_and_acquires_in_request_order(
    pg_sessions, monkeypatch, slice_timeout
):
    monkeypatch.setattr(
        ownership, "WAIT_SLICE", text(f"SET LOCAL lock_timeout = '{slice_timeout}'")
    )
    engine = create_async_engine(
        pg_sessions.kw["bind"].url,  # a second engine: another process (one gate per engine)
        execution_options={"ownership_concurrency": 3},
    )
    other = async_sessionmaker(engine, expire_on_commit=False)
    pool = engine.sync_engine.pool
    contended = asyncio.Event()

    async def contention():
        contended.set()

    monkeypatch.setattr(ownership, "_contended", contention)

    release = asyncio.Event()

    async def wait():
        async with ownership.run_ownership(other, "heart-rate") as sessions:
            async with sessions.begin():
                pass
            await release.wait()
            return "acquired"

    async with ownership.run_ownership(pg_sessions, "heart-rate") as owner:
        async with owner.begin():  # HELD_LOCK sees the session lock on this backend
            assert pg_sessions.kw["bind"].sync_engine.pool.checkedout() == 1  # the shared pool
        connects = []
        event.listen(pool, "connect", lambda *args: connects.append(args))
        task = asyncio.create_task(wait())
        await asyncio.wait_for(contended.wait(), 30)
        await waiting_backends(pg_sessions)  # the probe's own pooled connection
        opened = len(connects)
        seen = []
        for _ in range(12):  # several 100 ms slices time out and requeue
            await asyncio.sleep(0.1)  # sampling over 1.2 s: one waiter across requeued slices
            seen += await waiting_backends(pg_sessions)

        # On a loaded server the twelve looks above can all land between two slices; waiting
        # to observe the queue itself keeps what this asserts (one backend, in the lock queue).
        async def in_the_queue():
            found = await waiting_backends(pg_sessions)
            seen.extend(found)
            return "Lock" in {wait for _, wait in found}

        await until(in_the_queue, "the waiter in the server's lock queue")
        assert opened and len(connects) == opened
        assert len({pid for pid, _ in seen}) == 1
    if slice_timeout == "2s":
        # Released while the waiter is queued: a newcomer cannot jump ahead of it.
        async with ownership.run_ownership(pg_sessions, "heart-rate", wait=False) as late:
            assert late is None
    release.set()
    assert await asyncio.wait_for(task, 30) == "acquired"
    assert await waiting_backends(pg_sessions) == []
    await engine.dispose()
