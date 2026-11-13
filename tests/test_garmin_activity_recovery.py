"""Periodic pulls recover interrupted activity conversions from immutable evidence."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from garmin_records import FakeGarmin, activity, store_page
from sqlalchemy import event, select
from support.garmin import unlocked
from test_garmin_activity_ids import LONG_IDS, PREFIX
from test_garmin_activity_legacy_ids import legacy_page, legacy_row
from test_garmin_activity_results import EVIDENCE, snapshot

from app.core.config import Settings
from app.core.normalization import payload_hash
from app.db.contracts import OwnershipLost
from app.garmin.client import GarminClient
from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.models.health import ExerciseSession
from app.services.providers import garmin_bounded, garmin_readers
from app.services.providers.garmin_kinds import BY_NAME
from app.sync.ownership import CheckedSessions

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
DAY = NOW.date()
PATH = BY_NAME["activities"].request(DAY, DAY)[0]


def pull_at(sessions, tmp_path, fake, now=NOW):
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-key")
    return unlocked(
        Settings(_env_file=None, garmin_lookback_days=1),
        sessions,
        client=GarminClient(key, http=httpx.AsyncClient(transport=fake.transport())),
        clock=lambda: now,
    )


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("completed_pages", [0, 2])
@pytest.mark.parametrize("delay", [timedelta(hours=1), timedelta(days=10)])
async def test_restart_completes_only_pending_pages_without_refetch_or_date_window(
    sessions, tmp_path, monkeypatch, failure, completed_pages, delay
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 2)
    fake = FakeGarmin()
    fake.bodies[PATH] = [activity(i, DAY) for i in range(1, 6)]
    original = garmin_bounded.normalize_activities_page
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await original(*args, **kwargs)
        if calls == completed_pages + 1:
            raise failure("synthetic interruption after conversion, before commit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(garmin_bounded, "normalize_activities_page", interrupted)
        runner = pull_at(sessions, tmp_path, fake)
        try:
            with pytest.raises(failure):
                await runner.run(["activities"])
        finally:
            await runner.aclose()
    before = await snapshot(sessions, EVIDENCE)
    assert len(before[GarminRawRecord]) == 5
    assert len(before[GarminActivityResult]) == completed_pages * 2
    fake.bodies[PATH] = []
    runner = pull_at(sessions, tmp_path, fake, NOW + delay)
    loaded = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        # Inspect the selected columns, not JSON expressions inside ranking subqueries.
        compiled = context.compiled
        if compiled is not None and compiled.statement.is_select:
            columns = [column.name for column in compiled.statement.selected_columns]
            if columns == ["key", "body", "id"]:
                loaded.extend(parameters)
                assert len(parameters) <= 2

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        recovered = await runner.run(["activities"])
        assert not recovered.failed
        assert recovered.normalized.inserted == 5 - completed_pages * 2
        async with sessions() as session:
            visible = await garmin_readers.workouts(
                session, NOW - timedelta(days=1), NOW + timedelta(days=1)
            )
            assert len(visible) == 5
        after = await snapshot(sessions, (*EVIDENCE[1:], ExerciseSession))
        assert after[GarminRawRecord] == before[GarminRawRecord]
        assert all(row in after[GarminActivityResult] for row in before[GarminActivityResult])
        assert len(after[GarminActivityResult]) == 5
        assert set(loaded) == {row.raw_record_id for row in after[GarminActivityResult]} - {
            row.raw_record_id for row in before[GarminActivityResult]
        }
        loaded.clear()
        await runner.run(["activities"])
        assert await snapshot(sessions, (*EVIDENCE[1:], ExerciseSession)) == after
        assert loaded == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        await runner.aclose()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("numeric", [False, True])
async def test_pending_versions_and_reversions_use_last_full_identity_result(
    sessions, monkeypatch, legacy, observed, numeric
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 1)
    identifiers = (*LONG_IDS, PREFIX)
    if numeric:
        identifiers = tuple(map(int, identifiers))
    a, other, short = [activity(identifier, DAY) for identifier in identifiers]
    records = [a, other, short]
    if observed:
        if legacy:
            await legacy_page(sessions, monkeypatch, records, NOW)
        else:
            await store_page(sessions, "activities", records, NOW)
    else:
        async with sessions.begin() as session:
            session.add_all(
                GarminRawRecord(
                    kind="activities",
                    key=str(record["activityId"])[:64]
                    if legacy
                    else BY_NAME["activities"].key(record),
                    body=record,
                    body_sha256=payload_hash(record),
                    first_seen_at=NOW,
                )
                for record in records
            )
    changed, invalid = {**a, "calories": 333}, {**a, "duration": 0}
    for index, record in enumerate((a, changed, a, invalid, invalid, a)):
        if index:
            # Equal timestamps still select by observation id, including a return to raw A.
            await store_page(sessions, "activities", [record], NOW)
        stats = await garmin_bounded.normalize_bounded(
            sessions,
            DAY + timedelta(days=10),
            DAY + timedelta(days=10),
            device="Garmin",
            seen_since=NOW + timedelta(hours=index + 1),
        )
        assert stats.inserted == (3 if index == 0 else 0)
        assert stats.updated == (1 if index in (1, 2) else 0)
        assert stats.skipped == (1 if index == 3 else 0)
        saved = await snapshot(sessions, (GarminActivityResult, ExerciseSession))
        assert len(saved[GarminActivityResult]) == (3, 4, 5, 6, 6, 7)[index]
        async with sessions() as session:
            visible = await garmin_readers.workouts(
                session, NOW - timedelta(days=1), NOW + timedelta(days=1)
            )
            assert len(visible) == (2 if index in (3, 4) else 3)
        if index in (2, 5):
            results = [
                row
                for row in saved[GarminActivityResult]
                if row.source_record_id == f"activities:{BY_NAME['activities'].key(a)}"
            ]
            assert results[-1].raw_record_id == results[0].raw_record_id
    assert len((await snapshot(sessions, (GarminRawRecord,)))[GarminRawRecord]) == 5


@pytest.mark.parametrize("invalid", [False, True])
async def test_completed_legacy_results_are_not_pending_by_truncated_source_key(
    sessions, monkeypatch, invalid
):
    records = [activity(identifier, DAY) for identifier in (*LONG_IDS, PREFIX)]
    if invalid:
        records[1]["duration"] = 0
    await legacy_page(sessions, monkeypatch, records, NOW)
    async with sessions.begin() as session:
        raw = (await session.scalars(select(GarminRawRecord).order_by(GarminRawRecord.id))).all()
        session.add_all(legacy_row(record) for record in records if record["duration"] > 0)
        session.add_all(
            GarminActivityResult(
                source_record_id=f"activities:{PREFIX}",
                raw_record_id=row.id,
                normalized_key=(
                    f"garmin:activities:{record['activityId']}:v2"
                    if record["duration"] > 0
                    else None
                ),
            )
            for row, record in zip(raw, records, strict=True)
        )
    before = await snapshot(sessions, (GarminActivityResult, ExerciseSession))
    stats = await garmin_bounded.normalize_bounded(
        sessions, DAY, DAY, device="Garmin", seen_since=NOW + timedelta(hours=1)
    )
    assert stats.inserted == stats.updated == stats.unchanged == stats.skipped == 0
    assert await snapshot(sessions, (GarminActivityResult, ExerciseSession)) == before


@pytest.mark.parametrize("failure", [OwnershipLost, asyncio.CancelledError])
async def test_pending_recovery_stops_writing_when_ownership_or_task_is_lost(
    sessions, tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 1)
    await store_page(sessions, "activities", [activity(i, DAY) for i in (1, 2, 3)], NOW)
    begun = 0

    async def boundary():
        nonlocal begun
        begun += 1
        if begun == 3:  # Daily chunk and first activity committed; next page cannot start.
            raise failure("synthetic lost recovery owner")

    @asynccontextmanager
    async def owner(kind):
        yield CheckedSessions(sessions, boundary) if kind == "normalize" else sessions

    runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=10)).owned_by(owner)
    try:
        with pytest.raises(failure):
            await runner.run(["activities"])
    finally:
        await runner.aclose()
    saved = await snapshot(sessions, (GarminActivityResult, ExerciseSession))
    assert all(len(rows) == 1 for rows in saved.values())
    assert (await runner.status())["normalization"]["status"] == "unknown"
    runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=11))
    try:
        assert (await runner.run(["activities"])).normalized.inserted == 2
        recovered = await snapshot(sessions, (GarminActivityResult, ExerciseSession))
        assert all(len(rows) == 3 for rows in recovered.values())
        assert all(row in recovered[GarminActivityResult] for row in saved[GarminActivityResult])
    finally:
        await runner.aclose()
