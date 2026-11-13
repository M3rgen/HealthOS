"""Both lanes share one cycle and preserve response evidence on interrupted writes."""

import ast
import asyncio
from pathlib import Path

import httpx
import pytest
from raw_response_records import body, client, stored
from retrieval_records import Client, Runner, point
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from support.retrieval import rows, status

from app.google_health.client import Page
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.services.providers import ingestion
from app.services.synchronization.retrieval import ReceiptWriter, token_hash
from app.sync import backfill, errors, incremental, page_cycle


def test_only_the_shared_cycle_iterates_ingestion_pages():
    for module in (incremental, backfill):
        tree = ast.parse(Path(module.__file__).read_text())
        assert not any(isinstance(node, ast.AsyncFor) for node in ast.walk(tree))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert sum(isinstance(n.func, ast.Name) and n.func.id == "PageCycle" for n in calls) == 1
        assert not any(
            (isinstance(n.func, ast.Name) and n.func.id in {"ingest_pages", "iter_pages"})
            or (isinstance(n.func, ast.Attribute) and n.func.attr in {"store_raw", "committed"})
            for n in calls
        )
    tree = ast.parse(Path(page_cycle.__file__).read_text())
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFor)]
    assert len(loops) == 1 and loops[0].iter.func.id == "ingest_pages"


@pytest.mark.parametrize("lane", ["sync", "backfill"])
async def test_each_committed_page_advances_the_receipt_chain(sessions, lane):
    runner = Runner(sessions, lane)
    await runner.run(Client(Page([point()], "next"), Page([point()], None)))
    receipts, events = await rows(sessions)
    assert len(receipts) == 1 and receipts[0].lane == lane
    assert [e.kind for e in events] == ["page", "page", "completed"]
    assert [e.request_token_hash for e in events[:2]] == [None, token_hash("next")]
    assert [e.next_token_hash for e in events[:2]] == [token_hash("next"), None]
    assert (await status(sessions, runner))["retrieval"] == "completed_with_records"


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_interrupted_store_archives_before_outcome_and_keeps_durable_progress(
    sessions, monkeypatch, lane, cancel
):
    runner = Runner(sessions, lane)
    records = [point(), point(), point()]
    wires = [body(records[0], token="next"), body(*records[1:])]
    responses = iter(wires)
    google = client(lambda _: httpx.Response(200, content=next(responses)), sessions)
    store, failure = ingestion.store_page, ReceiptWriter.failure
    writing, release = asyncio.Event(), asyncio.Event()
    order = []

    async def interrupted(session, spec, page, **options):
        stats = await store(session, spec, page, **options)
        if page.next_page_token is None:
            order.append("store")
            writing.set()
            await release.wait()
            raise RuntimeError("synthetic failed transaction")
        return stats

    async def outcome(receipt, exc):
        # The first page committed; the rolled-back second page's wire bytes were
        # re-archived before a failed/cancelled receipt could be written.
        assert [r.body for r in await stored(sessions)] == wires
        order.append("outcome")
        await failure(receipt, exc)

    monkeypatch.setattr(ingestion, "store_page", interrupted)
    monkeypatch.setattr(ReceiptWriter, "failure", outcome)
    async with google.http:
        task = asyncio.create_task(runner.run(google))
        try:
            await asyncio.wait_for(writing.wait(), 30)
            if cancel:
                task.cancel()
            else:
                release.set()
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert order == ["store", "outcome"]
    receipts, events = await rows(sessions)
    assert len(receipts) == 1
    assert [e.kind for e in events] == ["page", "cancelled" if cancel else "failed"]
    assert [e.returned_count for e in events] == [1, 2]
    assert not any(e.kind == "completed" for e in events)
    assert (await status(sessions, runner))["retrieval"] == "incomplete"
    async with sessions() as session:
        raw = list(await session.scalars(select(RawHealthData)))
        assert [r.raw_json for r in raw] == records[:1]
        state = await session.get(
            SyncState if lane == "sync" else BackfillJob,
            runner.kind if lane == "sync" else runner.job_id,
        )
        token = state.last_page_token if lane == "sync" else state.page_token
        assert token == "next" and state.records_downloaded == 1
        assert state.status == (JobStatus.running if cancel else JobStatus.failed)
        if lane == "backfill":
            assert state.retry_count == (0 if cancel else 1)


@pytest.mark.parametrize("lane", ["sync", "backfill"])
async def test_database_conflict_retries_the_whole_page_before_progress(
    sessions, monkeypatch, lane
):
    class Conflict(Exception):
        sqlstate = "40001"

    store, attempts = ingestion.store_page, []

    async def conflicting(session, spec, page, **options):
        attempts.append(options["fetch_started"])
        stats = await store(session, spec, page, **options)
        if len(attempts) == 1:
            raise DBAPIError("synthetic", {}, Conflict())
        return stats

    monkeypatch.setattr(ingestion, "store_page", conflicting)
    monkeypatch.setattr(errors, "CONFLICT_BACKOFF", 0)
    runner, client = Runner(sessions, lane), Client(Page([point()], None))
    await runner.run(client)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert client.tokens == [None] and client.kept == []
    _, events = await rows(sessions)
    assert [e.kind for e in events] == ["page", "completed"]
    assert events[0].returned_count == 1
    async with sessions() as session:
        state = (await session.scalars(select(SyncState if lane == "sync" else BackfillJob))).one()
        assert state.records_downloaded == 1 and state.status == JobStatus.completed
        assert len(list(await session.scalars(select(RawHealthData)))) == 1
