"""Bounded Garmin normalization: chunked transactions, paged activities, bulk updates."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from garmin_records import activity, daily_steps, store_page
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.normalization import payload_hash
from app.db.base import Base
from app.models.garmin import (
    GarminActivityResult,
    GarminHeartMember,
    GarminHeartSet,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import ExerciseSession, HeartRate, Steps
from app.services.providers import garmin_bounded, garmin_normalize
from app.services.providers.garmin_ingestion import GarminSource
from app.services.providers.garmin_normalize import (
    newest_records,
    normalize_window,
    write_rows,
)
from app.services.providers.garmin_normalize_types import NormalizeStats
from app.services.providers.garmin_pull import day_window

FIRST = date(2026, 1, 1)
NOW = datetime(2026, 2, 1, tzinfo=UTC)


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bounded.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def steps_body(day: date, count: int) -> dict[str, Any]:
    return {"calendarDate": day.isoformat(), "totalSteps": count, "totalDistance": count}


def heart_body(day: date, bpm: int) -> dict[str, Any]:
    midnight = datetime.combine(day, datetime.min.time(), UTC)
    start = int(midnight.timestamp() * 1000)
    return {
        "calendarDate": day.isoformat(),
        "startTimestampGMT": f"{day.isoformat()}T00:00:00.0",
        "endTimestampGMT": f"{day.isoformat()}T23:59:59.0",
        "heartRateValues": [[start + index * 60000, bpm + index] for index in range(3)],
    }


async def archive(sessions, kind, key, body, digest):
    async with sessions.begin() as session:
        session.add(
            GarminRawRecord(kind=kind, key=key, body=body, body_sha256=digest, first_seen_at=NOW)
        )


async def archive_days(sessions, days, count=100, first=FIRST):
    for offset in range(days):
        day = first + timedelta(days=offset)
        await archive(sessions, "steps", day.isoformat(), steps_body(day, count), f"s{count}{day}")
        await archive(
            sessions, "heart-rate", day.isoformat(), heart_body(day, 60), f"h{count}{day}"
        )


def source(sessions) -> GarminSource:
    return GarminSource(Settings(_env_file=None), sessions)


class CountingSessions:
    """How many transactions a run opened, and how many statements each carried."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.begun = 0

    def __call__(self):
        return self.sessions()

    def begin(self):
        self.begun += 1
        return self.sessions.begin()


async def activity_projection(session):
    found = []
    for model in (ExerciseSession, GarminActivityResult):
        columns = [
            column
            for column in model.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        ]
        found.append(
            (await session.execute(select(*columns).order_by(model.source_record_id))).all()
        )
    return found


@pytest.mark.parametrize("start", [date(2000, 1, 1), date(2020, 1, 1)])
async def test_multiyear_activity_pages_never_run_daily_converters(sessions, monkeypatch, start):
    end = FIRST
    bodies = [
        activity(1, start),
        activity(2, end),
        activity(3, start - timedelta(days=1)),
        activity(4, end + timedelta(days=1)),
        activity(5, start - timedelta(days=2)),
        activity(6, end + timedelta(days=2)),
        {**activity(7, start), "duration": 0},
        {**activity(8, end), "duration": "1e1000", "endTimeGMT": f"{end}T18:00:00Z"},
        activity("full-identity-" + "x" * 80, start),
    ]
    await store_page(sessions, "activities", bodies, NOW)
    window = garmin_bounded.ActivityWindow(start, end, "Synthetic Garmin")

    def daily_converter_called(*args, **kwargs):
        pytest.fail("activity pages must not run daily normalization")

    for run in range(2):
        # Roll back the direct reference so both paths start from the same stored state.
        async with sessions() as session:
            direct = await normalize_window(session, start, end, device=window.device)
            expected = await activity_projection(session)
            await session.rollback()
        assert (direct.inserted, direct.updated, direct.unchanged, direct.skipped) == (
            6 if run == 0 else 0,
            0,
            6 if run else 0,
            1,
        )
        assert [len(rows) for rows in expected] == [6, 7]
        total, after, pages = NormalizeStats(), None, 0
        with monkeypatch.context() as guarded:
            for name in ("day_zones", "nearest_zone", "rows_for_day"):
                guarded.setattr(garmin_normalize, name, daily_converter_called)
            while True:
                async with sessions.begin() as session:
                    stats, after = await garmin_bounded.normalize_activities_page(
                        session, window, after=after, size=2
                    )
                total.add(stats)
                pages += 1
                if after is None:
                    break
        assert pages == 5
        assert total == direct
        async with sessions() as session:
            assert await activity_projection(session) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [123, True, False, "text", {}, None, []])
async def test_a_long_window_is_one_transaction_per_chunk(sessions, collection):
    await archive_days(sessions, 20)
    broken_day = FIRST + timedelta(days=3)
    body = {**heart_body(broken_day, 60), "heartRateValues": collection, "restingHeartRate": 50}
    await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=1))
    await store_page(sessions, "activities", [activity(1, FIRST + timedelta(days=19))], NOW)
    counting = CountingSessions(sessions)

    stats = await garmin_bounded.normalize_bounded(
        counting, FIRST, FIRST + timedelta(days=19), device="Garmin"
    )

    assert counting.begun == 3 + 1  # 7 + 7 + 6 days, then one page of activities
    assert stats.inserted == 20 * (1 + 1 + 3) - 3 + 2  # missing samples; resting + activity
    assert stats.tables["steps"]["inserted"] == 20
    assert stats.tables["distance"]["inserted"] == 20
    assert stats.tables["resting_heart_rate"]["inserted"] == 1
    assert stats.tables["exercise_sessions"]["inserted"] == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Steps)) == 20


async def test_duplicate_heart_keys_stay_stable_across_chunks(sessions):
    await archive_days(sessions, 8)
    for offset in (0, 7):
        day = FIRST + timedelta(days=offset)
        body = heart_body(day, 61)
        moment = body["heartRateValues"][0][0]
        body["heartRateValues"] = [[moment, 10**30], [moment, 61], [moment, 72]]
        await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=1))
    for run in range(4):
        stats = await garmin_bounded.normalize_bounded(
            sessions, FIRST, FIRST + timedelta(days=7), device="Garmin"
        )
        assert stats.skipped == 2
        assert stats.tables["heart_rate"] == {
            "inserted": 20 if run == 0 else 0,
            "updated": 0,
            "unchanged": 20 if run else 0,
        }
        async with sessions() as session:
            for model in (HeartRate, GarminHeartMember, GarminHeartSet):
                count = await session.scalar(select(func.count()).select_from(model))
                assert count == (8 if model is GarminHeartSet else 20)
            assert (
                await session.scalars(select(HeartRate.bpm).order_by(HeartRate.timestamp))
            ).first() == 61


@pytest.mark.asyncio
async def test_chunks_commit_so_an_interrupted_run_resumes_writing_only_the_rest(
    sessions, monkeypatch, unlocked_pull
):
    await archive_days(sessions, 15)
    window = FIRST, FIRST + timedelta(days=14)
    pull = unlocked_pull(Settings(_env_file=None), sessions, clock=lambda: NOW)
    async with sessions() as session:
        raw_before = (await session.execute(select(GarminRawRecord.__table__))).all()
    real = garmin_bounded.normalize_window
    calls = 0

    async def failing(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await real(*args, **kwargs)
        if calls == 3:
            raise RuntimeError("synthetic interruption")
        return result

    monkeypatch.setattr(garmin_bounded, "normalize_window", failing)
    with pytest.raises(RuntimeError, match="synthetic"):
        await pull.normalize(*window)
    async with sessions() as session:
        kept = await session.scalar(select(func.count()).select_from(Steps))
    assert kept == 14, "the two chunks before the interruption stay committed"
    assert (await pull.status())["normalization"]["status"] == "degraded"

    monkeypatch.setattr(garmin_bounded, "normalize_window", real)
    resumed = await pull.normalize(*window)
    assert resumed.inserted == 1 * 5, "only the missing chunk is written"
    assert resumed.unchanged == 14 * 5
    again = await pull.normalize(*window)
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 15 * 5)
    assert (await pull.status())["normalization"]["status"] == "healthy"
    async with sessions() as session:
        assert (await session.execute(select(GarminRawRecord.__table__))).all() == raw_before


@pytest.mark.asyncio
async def test_activities_are_paged_newest_version_per_key(sessions, monkeypatch):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 2)

    async def record(identifier, *, start, version=1):
        await archive(
            sessions,
            "activities",
            str(identifier),
            {
                "activityId": identifier,
                "startTimeGMT": f"{start.isoformat()} 10:00:00",
                "endTimeGMT": f"{start.isoformat()} 11:00:00",
                "duration": 3600.0 + version,
                "activityType": {"typeKey": "running"},
                "calories": version,
                "distance": 1000.0,
            },
            f"activity-{identifier}-{version}",
        )

    await record(1, start=FIRST)
    await record(2, start=FIRST, version=1)
    await record(2, start=FIRST, version=2)  # the newest version of activity 2
    await record(3, start=date(2020, 1, 1))  # outside the window, in a page of its own
    await record(4, start=FIRST + timedelta(days=1))
    counting = CountingSessions(sessions)

    stats = await garmin_bounded.normalize_bounded(
        counting, FIRST, FIRST + timedelta(days=1), device="Garmin"
    )

    assert counting.begun == 1 + 3  # one day chunk; pages {1, 2}, {3, 4} and an empty one
    assert stats.tables["exercise_sessions"]["inserted"] == 3
    async with sessions() as session:
        two = await session.scalar(
            select(ExerciseSession).where(
                ExerciseSession.google_data_point_id == "garmin:activities:2:v2"
            )
        )
    assert float(two.calories_kcal) == 2.0, "the newest version wins, not the first page's row"


@pytest.mark.asyncio
async def test_seen_since_recovers_pending_then_limits_completed_activities(sessions):
    await archive(
        sessions,
        "activities",
        "5",
        {
            "activityId": 5,
            "startTimeGMT": f"{FIRST.isoformat()} 10:00:00",
            "endTimeGMT": f"{FIRST.isoformat()} 11:00:00",
            "duration": 3600.0,
            "activityType": {"typeKey": "running"},
            "calories": 1,
            "distance": 1000.0,
        },
        "old",
    )
    later = NOW + timedelta(days=1)
    stats = await garmin_bounded.normalize_bounded(
        sessions, FIRST, FIRST, device="Garmin", seen_since=later
    )
    assert stats.tables["exercise_sessions"]["inserted"] == 1
    stats = await garmin_bounded.normalize_bounded(
        sessions, FIRST, FIRST, device="Garmin", seen_since=later
    )
    assert "exercise_sessions" not in stats.tables


@pytest.mark.asyncio
async def test_newest_records_loads_only_the_newest_body_per_key(sessions):
    await archive(sessions, "steps", FIRST.isoformat(), steps_body(FIRST, 1), "old")
    await archive(sessions, "steps", FIRST.isoformat(), steps_body(FIRST, 2), "new")
    loaded: list[int] = []

    async with sessions.begin() as session:
        connection = await session.connection()

        def count_rows(conn, cursor, statement, params, context, executemany):
            if "body" in statement and "garmin_raw_records" in statement:
                loaded.append(1)

        event.listen(connection.sync_connection, "before_cursor_execute", count_rows)
        latest = await newest_records(session, ("steps",), FIRST, FIRST)
        event.remove(connection.sync_connection, "before_cursor_execute", count_rows)

    assert latest["steps"][FIRST.isoformat()]["totalSteps"] == 2
    assert loaded == [1], "one statement reads the newest bodies"


@pytest.mark.asyncio
async def test_changed_rows_are_updated_in_groups_not_one_statement_each(sessions):
    await archive_days(sessions, 7, count=100)
    async with sessions.begin() as session:
        await normalize_window(session, FIRST, FIRST + timedelta(days=6))
    await archive_days(sessions, 7, count=200)

    async with sessions.begin() as session:
        connection = await session.connection()
        updates: list[int] = []

        def capture(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE"):
                updates.append(len(params) if executemany else 1)

        event.listen(connection.sync_connection, "before_cursor_execute", capture)
        stats = await normalize_window(session, FIRST, FIRST + timedelta(days=6))
        event.remove(connection.sync_connection, "before_cursor_execute", capture)

    assert stats.updated == 7 * 2  # steps and distance; the heart-rate samples are unchanged
    assert sum(updates) == 7 * 2 and len(updates) == 2, "one statement per table"
    async with sessions() as session:
        assert {row.count for row in await session.scalars(select(Steps))} == {200}


def test_stats_add_sums_every_counter_per_table():
    total = NormalizeStats()
    first = NormalizeStats(skipped=1)
    first.note("steps", 1, 2, 3)
    second = NormalizeStats(skipped=2)
    second.note("steps", 10, 20, 30)
    second.note("heart_rate", 5, 0, 0)
    total.add(first)
    total.add(second)
    assert (total.inserted, total.updated, total.unchanged, total.skipped) == (16, 22, 33, 3)
    assert total.tables == {
        "steps": {"inserted": 11, "updated": 22, "unchanged": 33},
        "heart_rate": {"inserted": 5, "updated": 0, "unchanged": 0},
    }


def test_day_chunks_cover_the_range_exactly():
    chunks = list(garmin_bounded.day_chunks(FIRST, FIRST + timedelta(days=14), 7))
    assert [(a.day, b.day) for a, b in chunks] == [(1, 7), (8, 14), (15, 15)]
    assert list(garmin_bounded.day_chunks(FIRST, FIRST - timedelta(days=1))) == []


@pytest.mark.asyncio
async def test_write_rows_executemany_keeps_values_per_row(sessions):
    async with sessions.begin() as session:
        await write_rows(session, HeartRate, [_sample("a", 60), _sample("b", 61)])
    async with sessions.begin() as session:
        await write_rows(session, HeartRate, [_sample("a", 70), _sample("b", 71)])
    async with sessions() as session:
        rows = dict(
            (await session.execute(select(HeartRate.google_data_point_id, HeartRate.bpm)))
            .tuples()
            .all()
        )
    assert {key: float(value) for key, value in rows.items()} == {
        "garmin:hr:a:v2": 70.0,
        "garmin:hr:b:v2": 71.0,
    }


def _sample(name: str, bpm: int) -> dict[str, Any]:
    return {
        "google_data_point_id": f"garmin:hr:{name}:v2",
        "provider": "GARMIN",
        "source_record_id": f"heart-rate:{name}",
        "device": "Garmin",
        "source_timezone": None,
        "timestamp": NOW,
        "bpm": bpm,
        "motion_context": None,
        "is_quarantined": False,
        "quarantine_reason": None,
    }


async def test_paged_activity_reversions_follow_observation_cutoff_and_keep_page_bounds(
    sessions, monkeypatch
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 2)
    counting = CountingSessions(sessions)
    engine = sessions.kw["bind"].sync_engine
    statements = []

    def capture(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    for index, value in enumerate((100, 200, 100, 100)):
        seen = NOW + timedelta(minutes=index)
        bodies = [{**activity(key, FIRST), "calories": value} for key in range(1, 6)]
        stored = await store_page(sessions, "activities", bodies, seen)
        assert stored.inserted == (5 if index < 2 else 0)
        counting.begun = 0
        statements.clear()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            stats = await garmin_bounded.normalize_bounded(
                counting, FIRST, FIRST, device="Garmin", seen_since=seen
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert counting.begun == 5  # daily window, activity pages of 2, 2, 1, pending daily scan
        assert all("garmin_api_responses" not in sql for sql in statements)
        assert stats.updated == (5 if index in (1, 2) else 0)
        async with sessions() as session:
            rows = (await session.scalars(select(ExerciseSession))).all()
            assert len(rows) == 5 and {row.calories_kcal for row in rows} == {value}
    assert stats.unchanged == 5 and stats.inserted == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GarminRawRecord)) == 10
        assert await session.scalar(select(func.count()).select_from(GarminRecordObservation)) == 20
    missed = await garmin_bounded.normalize_bounded(
        sessions, FIRST, FIRST, device="Garmin", seen_since=seen + timedelta(seconds=1)
    )
    assert "exercise_sessions" not in missed.tables
    replay = await garmin_bounded.normalize_bounded(sessions, FIRST, FIRST, device="Garmin")
    assert replay.updated == replay.inserted == 0 and replay.unchanged == 5


@pytest.mark.parametrize("kind", ["steps", "activities"])
async def test_legacy_keys_keep_max_id_until_their_first_real_observation(sessions, kind):
    def body(key, value):
        return (
            daily_steps(key, steps=value)
            if kind == "steps"
            else {**activity(key, FIRST), "calories": value}
        )

    keys = [FIRST, FIRST + timedelta(days=1)] if kind == "steps" else [1, 2]
    for key in keys:
        for value in (100, 200):
            record = body(key, value)
            await archive(sessions, kind, str(key), record, payload_hash(record))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GarminRecordObservation)) == 0
    end = FIRST + timedelta(days=1)
    legacy = await source(sessions).normalize(day_window(FIRST, end))
    model = Steps if kind == "steps" else ExerciseSession
    assert legacy.tables[model.__tablename__]["inserted"] == 2
    later = NOW + timedelta(days=1)
    stored = await store_page(sessions, kind, [body(keys[0], 100)], later)
    assert stored.inserted == 0
    updated = await source(sessions).normalize(day_window(FIRST, end), seen_since=later)
    assert updated.tables[model.__tablename__]["updated"] == 1
    async with sessions() as session:
        rows = (await session.scalars(select(model).order_by(model.source_record_id))).all()
        assert [row.count if kind == "steps" else row.calories_kcal for row in rows] == [100, 200]
        assert await session.scalar(select(func.count()).select_from(GarminRawRecord)) == 4
        assert await session.scalar(select(func.count()).select_from(GarminRecordObservation)) == 1
    again = await source(sessions).normalize(day_window(FIRST, end))
    assert again.updated == again.inserted == 0


async def test_heart_membership_commits_with_each_chunk_and_resumes_after_failure(
    sessions, monkeypatch
):
    last = FIRST + timedelta(days=8)
    days = [FIRST + timedelta(days=i) for i in range(9)]
    for day in days:
        await store_page(sessions, "heart-rate", [heart_body(day, 60)], NOW)
    await source(sessions).normalize(day_window(FIRST, last))
    for day in days:
        body = heart_body(day, 70)
        body["heartRateValues"] = body["heartRateValues"][:1]
        await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=1))
    real = garmin_bounded.normalize_window

    async def interrupted(session, start, end, **kwargs):
        result = await real(session, start, end, **kwargs)
        if start > FIRST:
            raise RuntimeError("synthetic chunk rollback after membership")
        return result

    monkeypatch.setattr(garmin_bounded, "normalize_window", interrupted)
    with pytest.raises(RuntimeError, match="synthetic chunk"):
        await source(sessions).normalize(day_window(FIRST, last))
    from app.services.providers.garmin_readers import heart_rate_samples

    start = datetime.combine(FIRST, datetime.min.time(), UTC)
    stop = start + timedelta(days=9)
    async with sessions() as session:
        values = await heart_rate_samples(session, start, stop)
        assert len(values) == 7 + 2 * 3
        assert [bpm for _, bpm in values] == [70] * 7 + [60, 61, 62] * 2
        assert await session.scalar(select(func.count()).select_from(GarminHeartSet)) == 16
    monkeypatch.setattr(garmin_bounded, "normalize_window", real)
    # A fresh source has no in-memory normalization state.
    resumed = await source(sessions).normalize(
        day_window(NOW.date(), NOW.date()), seen_since=NOW + timedelta(days=1)
    )
    assert resumed.updated == 2 and resumed.inserted == 0
    async with sessions() as session:
        assert [bpm for _, bpm in await heart_rate_samples(session, start, stop)] == [70] * 9
        assert await session.scalar(select(func.count()).select_from(HeartRate)) == 27
        assert await session.scalar(select(func.count()).select_from(GarminHeartSet)) == 18
    again = await source(sessions).normalize(day_window(FIRST, last))
    assert again.inserted == again.updated == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GarminHeartSet)) == 18


async def test_heart_membership_uses_bounded_bulk_writes_and_constant_reader_queries(sessions):
    from app.services.providers.garmin_readers import heart_rate_samples

    start = datetime.combine(FIRST, datetime.min.time(), UTC)
    ms = int(start.timestamp() * 1000)
    body = {
        "calendarDate": str(FIRST),
        "heartRateValues": [[ms + i * 1000, 60] for i in range(2101)],
    }
    await store_page(sessions, "heart-rate", [body], NOW)
    engine = sessions.kw["bind"].sync_engine
    statements = []

    def capture(conn, cursor, statement, params, context, executemany):
        statements.append((statement, len(params) if executemany else 1))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        await source(sessions).normalize(day_window(FIRST, FIRST))
        member_writes = [
            (sql, size)
            for sql, size in statements
            if sql.startswith("INSERT INTO garmin_heart_members")
        ]
        assert [size for _, size in member_writes] == [1000, 1000, 101]
        statements.clear()
        async with sessions() as session:
            assert len(await heart_rate_samples(session, start, start + timedelta(days=1))) == 2101
        assert len(statements) == 1
        assert (
            "garmin_raw_records" not in statements[0][0]
            and "garmin_api_responses" not in statements[0][0]
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    body["heartRateValues"] = body["heartRateValues"][:1001]
    await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=1))
    await source(sessions).normalize(day_window(FIRST, FIRST))
    async with sessions() as session:
        assert len(await heart_rate_samples(session, start, start + timedelta(days=1))) == 1001
        assert await session.scalar(select(func.count()).select_from(HeartRate)) == 2101
        assert await session.scalar(select(func.count()).select_from(GarminHeartMember)) == 3102
