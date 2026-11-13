"""Archive replay paging and measured memory growth with synthetic stored batches."""

import gc
import tracemalloc
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, update
from support.webhooks import Incremental, notification
from webhook_ownership_records import ingest, processor

from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification
from app.sync import webhook_reads
from app.sync.webhook_processing import ProcessingStats

STAMP = datetime(2026, 1, 1, tzinfo=UTC)


async def seed_batches(sessions, start, end):
    # Fixture generation is bounded too; raw columns must never be projected by archive scans.
    async with sessions.begin() as session:
        for offset in range(start, end, 256):
            await session.execute(
                insert(WebhookEvent),
                [
                    dict(
                        payload_hash=f"{index:064x}",
                        created_at=STAMP,
                        raw_json={},
                        raw_body=b"synthetic archive " * 128,
                    )
                    for index in range(offset, min(end, offset + 256))
                ],
            )


async def test_replay_hydrates_only_legacy_batches_and_visits_failed_work_once(
    sessions, monkeypatch
):
    runner = processor(sessions, None)
    _, completed = await ingest(runner)
    runner.incremental = Incremental()
    async with sessions.begin() as session:
        row = await session.get(WebhookNotification, completed)
        row.status = "completed"
    # A completed archived batch must still hydrate if it predates per-element state.
    async with sessions.begin() as session:
        for minute in range(7):
            session.add(
                WebhookEvent(
                    payload_hash=f"{minute:064x}",
                    raw_json=notification(minute=minute + 1),
                    created_at=STAMP,
                    processed_at=STAMP if minute == 0 else None,
                )
            )
    monkeypatch.setattr(webhook_reads, "READ_PAGE_SIZE", 2)
    prepared = []
    actual_prepare = runner.prepare_existing_batch

    async def prepare(batch_hash):
        prepared.append(batch_hash)
        return await actual_prepare(batch_hash)

    runner.prepare_existing_batch = prepare
    result = await runner.process_pending(recover_running=False)
    assert prepared == [f"{minute:064x}" for minute in range(7)]
    assert result.batches == 7  # six pending legacy batches and the original delivery
    assert result.completed_elements == 7  # includes the preserved completed element
    assert len(runner.incremental.calls) == 6
    # Pagination must advance even when failed batches remain in the query result.
    async with sessions.begin() as session:
        await session.execute(update(WebhookEvent).values(processed_at=None))
        await session.execute(update(WebhookNotification).values(status="failed"))

    async def failing(*args, **kwargs):
        raise RuntimeError("synthetic source failure")

    runner.incremental.run = failing
    retried = await runner.process_pending(recover_running=False)
    assert retried.batches == retried.failed_elements == 8
    assert len(prepared) == 7


async def test_page_reader_closes_sessions_and_defers_newer_deliveries(sessions, monkeypatch):
    await seed_batches(sessions, 0, 5)
    monkeypatch.setattr(webhook_reads, "READ_PAGE_SIZE", 2)
    seen = []
    async for key in webhook_reads.batch_hashes(sessions):
        seen.append(key)
        async with sessions.begin() as session:
            # Remove visited rows: OFFSET paging would now skip later entries.
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.payload_hash == key)
                .values(processed_at=STAMP)
            )
            if len(seen) == 1:
                session.add(
                    WebhookEvent(
                        payload_hash="f" * 64,
                        raw_json={},
                        created_at=STAMP + timedelta(seconds=1),
                    )
                )
    assert seen == [f"{index:064x}" for index in range(5)]
    assert [key async for key in webhook_reads.batch_hashes(sessions)] == ["f" * 64]


@pytest.mark.parametrize("count", [0, 1, 256, 257])
async def test_page_boundaries_and_prepared_membership(sessions, count):
    await seed_batches(sessions, 0, count)
    async with sessions.begin() as session:
        session.add(WebhookNotification(element_hash="shared", raw_json={}))
        await session.flush()
        for index in range(0, count, 2):
            session.add(
                WebhookBatchItem(batch_hash=f"{index:064x}", ordinal=1, element_hash="shared")
            )
    actual = [key async for key in webhook_reads.batch_hashes(sessions, unprepared=True)]
    assert actual == [f"{index:064x}" for index in range(1, count, 2)]
    assert len([key async for key in webhook_reads.batch_hashes(sessions)]) == count


async def test_replay_memory_is_bounded_when_archive_grows_sixteenfold(sessions, monkeypatch):
    runner = processor(sessions, None)
    prepared = processed = 0

    async def prepare(_key):
        nonlocal prepared
        prepared += 1

    async def process(_key):
        nonlocal processed
        processed += 1
        return ProcessingStats(batches=1)

    # Measure the full replay traversal, isolating the working set of the archive scans
    # from individual payload parsing/fetching and from a test-owned collection of keys.
    monkeypatch.setattr(runner, "prepare_existing_batch", prepare)
    monkeypatch.setattr(runner, "process_batch", process)
    read_count = largest_limit = 0

    def trace(_conn, _cursor, statement, parameters, _context, _executemany):
        nonlocal read_count, largest_limit
        if statement.lstrip().startswith("SELECT"):
            assert "LIMIT" in statement, statement
            assert "raw_json" not in statement and "raw_body" not in statement
            # SQLite emits OFFSET 0 after LIMIT; PostgreSQL ends with LIMIT alone.
            limit = parameters[-2 if _context.dialect.name == "sqlite" else -1]
            largest_limit = max(largest_limit, limit)
            read_count += 1

    async def measure(count):
        nonlocal prepared, processed, read_count, largest_limit
        prepared = processed = 0
        read_count = largest_limit = 0
        gc.collect()
        engine = sessions.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", trace)
        tracemalloc.start()
        try:
            result = await runner.process_pending(recover_running=False)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            event.remove(engine, "before_cursor_execute", trace)
        assert result.batches == prepared == processed == count
        assert read_count and largest_limit <= webhook_reads.READ_PAGE_SIZE
        return peak

    await seed_batches(sessions, 0, 512)
    await runner.process_pending(recover_running=False)  # warm SQL compilation caches
    small = await measure(512)
    await seed_batches(sessions, 512, 8192)
    large = await measure(8192)
    print(f"replay peak: 512 batches={small} B; 8192 batches={large} B")
    assert large < small + 512 * 1024
    assert large < 1024 * 1024
