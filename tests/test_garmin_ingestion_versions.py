"""Page version lookups stay bounded as immutable correction history grows."""

from datetime import timedelta
from math import ceil

import pytest
from garmin_records import daily_steps, store_page
from sqlalchemy import insert, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from test_garmin_activity_ids import LONG_IDS, body
from test_garmin_activity_results import EVIDENCE, snapshot
from test_garmin_normalize import DAY, NOW

from app.core.ingestion_stats import StoreStats
from app.core.normalization import payload_hash
from app.models.garmin import GarminRawRecord, GarminRecordObservation
from app.services.providers.garmin_kinds import BY_NAME


async def traced_store(sessions, monkeypatch, kind, records):
    execute, counts = AsyncSession.execute, []

    async def tracked(session, query, *args, **kwargs):
        result = await execute(session, query, *args, **kwargs)
        if query.is_select and GarminRawRecord.__table__ in query.get_final_froms():
            frozen = result.freeze()
            counts.append(len(frozen.data))  # Actual returned rows, not timing or rowcount=-1.
            for dialect in (sqlite.dialect(), postgresql.dialect()):
                compiled = query.compile(
                    dialect=dialect, compile_kwargs={"render_postcompile": True}
                )
                assert len(compiled.params) <= 801
            return frozen()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", tracked)
        stats = await store_page(sessions, kind, records, NOW)
    return stats, counts


@pytest.mark.parametrize("mode", ["steps", "short", "canonical", "legacy", "both"])
async def test_returned_versions_do_not_grow_with_archive_history(sessions, monkeypatch, mode):
    kind = "steps" if mode == "steps" else "activities"
    records = (
        [daily_steps(DAY + timedelta(days=i)) for i in range(2)]
        if mode == "steps"
        else [body(i if mode == "short" else identifier) for i, identifier in enumerate(LONG_IDS)]
    )
    records = [{**record, "revision": 0} for record in records]
    expected = [2, 2] if mode in ("steps", "short") else [4 if mode == "both" else 2, 2, 2]
    for start, stop in ((0, 8), (8, 512)):
        rows = []
        for record in records:
            keys = [BY_NAME[kind].key(record)]
            if mode in ("legacy", "both"):
                keys = ([keys[0]] if mode == "both" else []) + [str(record["activityId"])[:64]]
            for revision in range(start, stop):
                revised = {**record, "revision": revision}
                rows.extend(
                    dict(
                        kind=kind,
                        key=key,
                        body=revised,
                        body_sha256=payload_hash(revised),
                        first_seen_at=NOW,
                    )
                    for key in keys
                )
        async with sessions.begin() as session:
            await session.execute(insert(GarminRawRecord), rows)
            if start == 0:
                # Same digests in another kind, and cross-paired keys/digests, must not leak in.
                await session.execute(
                    insert(GarminRawRecord),
                    [
                        dict(
                            kind=other_kind,
                            key=BY_NAME[kind].key(record),
                            body=other,
                            body_sha256=payload_hash(other),
                            first_seen_at=NOW,
                        )
                        for record, other in zip(records, reversed(records), strict=True)
                        for other_kind in (kind, "hrv")
                    ],
                )
        before = await snapshot(sessions, (GarminRawRecord,))
        stats, counts = await traced_store(sessions, monkeypatch, kind, records * 2)
        assert stats == StoreStats() and counts == expected
        assert await snapshot(sessions, (GarminRawRecord,)) == before


@pytest.mark.parametrize("kind", ["steps", "activities"])
async def test_large_pages_query_by_chunks_and_keep_same_key_versions(sessions, monkeypatch, kind):
    records = [
        daily_steps(DAY + timedelta(days=i)) if kind == "steps" else body("8" * 64 + str(i))
        for i in range(805)
    ]
    records.append({**records[0], "correction": True})
    size = len(records)
    for inserted in (size, 0):
        stats, counts = await traced_store(sessions, monkeypatch, kind, records * 2)
        assert stats == StoreStats(inserted=inserted)
        assert len(counts) == 2 * ceil(size / 400) + (
            ceil(2 * size / 400) if kind == "activities" else 0
        )
        assert max(counts) <= 400
        assert sum(counts) == (size if inserted else size * (3 if kind == "activities" else 2))
    async with sessions() as session:
        observations = (
            await session.scalars(
                select(GarminRecordObservation.raw_record_id).order_by(GarminRecordObservation.id)
            )
        ).all()
        assert len(observations) == size * 2
        assert observations[:size] == observations[size:]


async def test_failure_after_later_lookup_batch_rolls_back_entire_page(sessions, monkeypatch):
    await store_page(sessions, "steps", [daily_steps(DAY)], NOW)
    before = await snapshot(sessions, EVIDENCE[:3])
    original = AsyncSession.execute
    seen = 0

    async def interrupted(session, query, *args, **kwargs):
        nonlocal seen
        result = await original(session, query, *args, **kwargs)
        if query.is_select and GarminRawRecord.__table__ in query.get_final_froms():
            seen += 1
            if seen == 6:  # All new rows and earlier lookup chunks are already in the transaction.
                raise RuntimeError("synthetic later lookup failure")
        return result

    monkeypatch.setattr(AsyncSession, "execute", interrupted)
    with pytest.raises(RuntimeError, match="synthetic later lookup"):
        await store_page(
            sessions, "steps", [daily_steps(DAY + timedelta(days=i)) for i in range(805)], NOW
        )
    assert await snapshot(sessions, EVIDENCE[:3]) == before
