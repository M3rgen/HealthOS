"""Every mutation invalidates bounds; hits preserve the uncached history contract."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from derived_records import BAND, manual, store
from history_cache_archive import TODAY, counted_reads
from sqlalchemy import update
from support.postgres_reads import read_statements
from test_metric_history import records

from app.db.dialect import database
from app.models.health import RestingHeartRate, SleepSession, Steps
from app.models.state import RawHealthData
from app.services.archive.manual_flags import record_flag
from app.services.dashboard_views import history_bounds
from app.services.dashboard_views.dashboard_history import HistoryPageNotFound, history_data
from app.services.providers.ingestion_readers import ARCHIVES

KINDS = {"steps": "steps", "sleep": "sleep", "heart": "daily-resting-heart-rate"}
MODELS = {"steps": Steps, "sleep": SleepSession, "heart": RestingHeartRate}
MIDDLE = TODAY - timedelta(days=35)


def payload(metric, name, day, invalid=False):
    body = records(metric, day, name)[0].raw_json
    body.update(name=f"synthetic/{KINDS[metric]}/{name}", dataSource=BAND)
    if invalid:
        if metric == "steps":
            body["steps"]["count"] = -1
        elif metric == "sleep":
            body["sleep"]["summary"]["minutesAsleep"] = -1
        else:
            body["dailyRestingHeartRate"]["beatsPerMinute"] = -1
    return body


async def read(sessions, metric="steps", **options):
    async with sessions() as session:
        return await history_data(session, metric, 1, "UTC", TODAY, **options)


async def checked(sessions, monkeypatch, metric, first, last, *, cold=True):
    with counted_reads(sessions, monkeypatch) as (sql, _):
        result = await read(sessions, metric)
    # GitHub-only Apple extension: + its bounds aggregate (cold only) and its page read.
    assert len(read_statements(sql)) == (7 + 2 if cold else 4 + 1), sql
    assert (result["earliest_date"], result["latest_date"]) == (first, last)
    assert result == await read(sessions, metric, use_bounds_cache=False)
    assert result == await read(sessions, metric)
    async with sessions() as session:
        with pytest.raises(HistoryPageNotFound):
            await history_data(session, metric, result["total_pages"] + 1, "UTC", TODAY)
    return result


@pytest.mark.parametrize("metric", KINDS)
@pytest.mark.parametrize("edge", ["oldest", "newest"])
@pytest.mark.parametrize(
    "mutation", ["correction", "raw_quarantine", "normalized_quarantine", "delete"]
)
async def test_endpoint_removal_and_restoration(sessions, monkeypatch, metric, edge, mutation):
    day = TODAY - timedelta(days=95 if edge == "oldest" else 2)
    await store(
        sessions, KINDS[metric], payload(metric, "middle", MIDDLE), payload(metric, "edge", day)
    )
    first, last = sorted((MIDDLE, day))
    await checked(sessions, monkeypatch, metric, first, last)
    for invalid in (True, False):
        if mutation == "correction":
            await store(sessions, KINDS[metric], payload(metric, "edge", day, invalid))
        else:
            model = MODELS[metric] if mutation == "normalized_quarantine" else RawHealthData
            values = (
                {"source_deleted_at": datetime(2026, 10, 1, tzinfo=UTC) if invalid else None}
                if mutation == "delete"
                else {"is_quarantined": invalid}
            )
            async with sessions.begin() as session:
                await session.execute(
                    update(model)
                    .where(model.google_data_point_id == f"synthetic/{KINDS[metric]}/edge")
                    .values(**values)
                )
        await checked(
            sessions, monkeypatch, metric, MIDDLE if invalid else first, MIDDLE if invalid else last
        )


@pytest.mark.parametrize("metric", KINDS)
async def test_insert_new_endpoints_and_manual_writers(sessions, monkeypatch, metric):
    await checked(sessions, monkeypatch, metric, None, None)
    oldest = TODAY - timedelta(days=120)
    for name, day in (("middle", MIDDLE), ("old", oldest), ("new", TODAY)):
        await store(sessions, KINDS[metric], payload(metric, name, day))
        await checked(
            sessions,
            monkeypatch,
            metric,
            oldest if name != "middle" else MIDDLE,
            day if name != "old" else MIDDLE,
        )
    # Manual profile measurements and flags do not contribute to these three metrics,
    # but are deliberately covered by the conservative database revision as well.
    identifier = await manual(sessions, 70)
    await checked(sessions, monkeypatch, metric, oldest, TODAY)
    for flagged in (True, False):
        async with sessions.begin() as session:
            await record_flag(session, identifier, "test", flagged)
        await checked(sessions, monkeypatch, metric, oldest, TODAY)


@pytest.mark.parametrize("seed", [7, 724, 2026])
@pytest.mark.parametrize("zone", ["UTC", "Europe/Brussels", "America/Los_Angeles"])
async def test_seeded_responses_equal_with_and_without_cache(sessions, seed, zone):
    rng = random.Random(seed)
    for metric, kind in KINDS.items():
        await store(
            sessions,
            kind,
            *(
                payload(
                    metric,
                    str(i),
                    TODAY - timedelta(days=rng.randrange(130)),
                    rng.choice((False, True)),
                )
                for i in range(40)
            ),
        )
        for page in (1, 2, 3, 4, 5):
            async with sessions() as session:
                try:
                    expected = await history_data(
                        session, metric, page, zone, TODAY, use_bounds_cache=False
                    )
                except HistoryPageNotFound:
                    with pytest.raises(HistoryPageNotFound):
                        await history_data(session, metric, page, zone, TODAY)
                else:
                    assert await history_data(session, metric, page, zone, TODAY) == expected
                    assert await history_data(session, metric, page, zone, TODAY) == expected


async def test_key_method_day_zone_custom_readers_and_lru(sessions, monkeypatch):
    await store(sessions, "steps", payload("steps", "edge", MIDDLE))
    await checked(sessions, monkeypatch, "steps", MIDDLE, MIDDLE)
    monkeypatch.setattr(
        history_bounds.interpretation.canonical_daily, "METHOD", "synthetic-method/724"
    )
    await checked(sessions, monkeypatch, "steps", MIDDLE, MIDDLE)
    monkeypatch.setattr(history_bounds, "_CACHE", history_bounds.BoundsCache(3))
    async with sessions() as session:
        for offset in range(6):
            for zone in ("UTC", "Europe/Brussels"):
                await history_data(session, "steps", 1, zone, TODAY + timedelta(days=offset))
                assert len(history_bounds._CACHE.entries) <= 3
        custom = replace(ARCHIVES[0], archive_bounds=AsyncMock(wraps=ARCHIVES[0].archive_bounds))
        for _ in range(2):
            await history_data(session, "steps", 1, "UTC", TODAY, archives=(custom,))
        assert custom.archive_bounds.await_count == 2


async def test_local_write_and_rollback_cannot_publish_speculative_bounds(sessions, monkeypatch):
    await store(sessions, "steps", payload("steps", "middle", MIDDLE))
    await checked(sessions, monkeypatch, "steps", MIDDLE, MIDDLE)
    async with sessions() as session:
        session.add_all(records("steps", TODAY, "pending"))
        result = await history_data(session, "steps", 1, "UTC", TODAY)
        assert result["latest_date"] == TODAY
        # Now the writes are flushed, so checking only session.dirty would be unsafe.
        assert not session.new and not session.dirty
        result = await history_data(session, "steps", 1, "UTC", TODAY)
        assert result["latest_date"] == TODAY
        await session.rollback()
    result = await read(sessions)
    assert result["earliest_date"] == result["latest_date"] == MIDDLE


@pytest.mark.parametrize("failure", ["error", "cancel", "changed"])
async def test_miss_does_not_publish_failure_or_changed_revision(sessions, monkeypatch, failure):
    import asyncio

    await store(sessions, "steps", payload("steps", "middle", MIDDLE))
    async with sessions() as session:
        revision = database(session).read_revision
        with monkeypatch.context() as patch:
            if failure == "changed":

                async def changed(target):
                    return ((await revision(target)), object())

                patch.setattr(database(session), "read_revision", changed)
            else:
                from app.services.interpretation import google_daily_readers

                def broken(*args):
                    raise (
                        asyncio.CancelledError() if failure == "cancel" else ValueError("synthetic")
                    )

                patch.setattr(google_daily_readers, "step_window", broken)
            if failure == "changed":
                await history_data(session, "steps", 1, "UTC", TODAY)
            else:
                with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
                    await history_data(session, "steps", 1, "UTC", TODAY)
    await checked(sessions, monkeypatch, "steps", MIDDLE, MIDDLE)
