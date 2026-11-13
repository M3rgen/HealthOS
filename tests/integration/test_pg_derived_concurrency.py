"""Concurrent writers cannot deadlock on ``derived_days``; a day is bumped once per commit.

The race is made deterministic with a barrier: each writer takes its first lock and waits
for the other before taking its second, the shape that produced PostgreSQL 40P01 when the
row triggers updated ``derived_days`` directly. No test relies on sleeps or timing.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic import command
from derived_records import steps as raw_steps
from derived_records import summary as raw_summary
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.providers import FIT, GARMIN, GOOGLE, garmin_record_key
from app.google_health.data_types import REGISTRY
from app.models.derived import DerivedDay, DerivedDayPending
from app.models.health import SleepSession, Steps
from app.services.archive.raw_history_verification import verify_guards
from app.services.archive.storage import store_records
from app.services.interpretation.derived import register_range

pytestmark = pytest.mark.integration

FIRST_DAY = date(2026, 9, 1)
TIMEZONE = "UTC"
DAYS = 10


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day, hours=hour)


def steps_row(day: int, provider: str = GOOGLE, key: str | None = None) -> Steps:
    identifier = key or f"synthetic-steps-{uuid.uuid4().hex}"
    return Steps(
        google_data_point_id=identifier,
        provider=provider,
        start_time=at(day),
        end_time=at(day) + timedelta(minutes=30),
        count=100,
    )


async def _register(sessions, days: int = DAYS) -> None:
    async with sessions.begin() as session:
        await register_range(session, FIRST_DAY, FIRST_DAY + timedelta(days=days - 1), TIMEZONE)


async def _revisions(sessions) -> dict[date, int]:
    async with sessions() as session:
        rows = await session.execute(select(DerivedDay.day, DerivedDay.revision))
        return dict(rows.tuples().all())


async def test_writers_touching_days_in_opposite_order_do_not_deadlock(pg_sessions):
    await _register(pg_sessions, 2)
    barrier = asyncio.Barrier(2)

    async def write(first: int, second: int):
        async with pg_sessions() as session, session.begin():
            session.add(steps_row(first))
            await session.flush()
            await barrier.wait()  # both hold what the first statement took, then cross
            session.add(steps_row(second))
            await session.flush()

    results = await asyncio.gather(write(0, 1), write(1, 0), return_exceptions=True)
    assert [r for r in results if isinstance(r, BaseException)] == []
    revisions = await _revisions(pg_sessions)
    assert revisions == {
        FIRST_DAY: 3,  # two commits touched it: 1 + 1 + 1
        FIRST_DAY + timedelta(days=1): 3,
    }


async def test_three_writers_over_shared_days_all_commit_and_count_each_page(pg_sessions):
    await _register(pg_sessions)
    rng = random.Random(20261108)
    conflicts: list[str] = []
    expected = dict.fromkeys(range(DAYS), 0)

    async def write(kind, records, days):
        try:
            async with pg_sessions() as session, session.begin():
                await store_records(session, REGISTRY[kind], records)
        except DBAPIError as exc:
            conflicts.append(getattr(exc.orig, "sqlstate", "?"))
            return
        for day in days:
            expected[day] += 1

    for _ in range(40):
        step_days = [rng.randrange(DAYS) for _ in range(120)]
        pages = (
            write(
                "steps",
                [
                    raw_steps(uuid.uuid4().hex, count=10, start=at(d, h % 23))
                    for h, d in enumerate(step_days)
                ],
                set(step_days),
            ),
            write(
                "daily-resting-heart-rate",
                [
                    raw_summary(
                        "daily-resting-heart-rate",
                        uuid.uuid4().hex,
                        day=FIRST_DAY + timedelta(days=d),
                        beatsPerMinute=60,
                    )
                    for d in rng.sample(range(DAYS), DAYS)
                ],
                set(range(DAYS)),
            ),
            write(
                "daily-oxygen-saturation",
                [
                    raw_summary(
                        "daily-oxygen-saturation",
                        uuid.uuid4().hex,
                        day=FIRST_DAY + timedelta(days=d),
                        averagePercentage=97.0,
                    )
                    for d in rng.sample(range(DAYS), DAYS)
                ],
                set(range(DAYS)),
            ),
        )
        await asyncio.gather(*pages)

    assert conflicts == []
    revisions = await _revisions(pg_sessions)
    assert revisions == {
        FIRST_DAY + timedelta(days=day): 1 + count for day, count in expected.items()
    }
    async with pg_sessions() as session:
        assert (await session.execute(select(DerivedDayPending))).first() is None


@pytest.mark.parametrize(
    ("model", "start", "end"),
    [(Steps, at(1), at(1) + timedelta(minutes=30)), (SleepSession, at(0), at(1))],
)
@pytest.mark.parametrize("provider", [GOOGLE, FIT])
async def test_garmin_rows_do_not_invalidate_but_other_providers_do(
    pg_sessions, model, start, end, provider
):
    await _register(pg_sessions, 2)
    before = await _revisions(pg_sessions)
    common = {"start_time": start, "end_time": end}
    google = {"count": 5} if model is Steps else {"duration_seconds": 3600, "awake_seconds": 0}
    garmin_key = garmin_record_key("steps" if model is Steps else "sleep", "2026-09-02", "v2")

    async with pg_sessions() as session, session.begin():
        session.add(model(google_data_point_id=garmin_key, provider=GARMIN, **common, **google))
    assert await _revisions(pg_sessions) == before

    async with pg_sessions() as session, session.begin():
        row = (await session.execute(select(model))).scalar_one()
        row.start_time = start - timedelta(minutes=1)
    assert await _revisions(pg_sessions) == before

    async with pg_sessions() as session, session.begin():
        session.add(
            model(google_data_point_id="synthetic-other", provider=provider, **common, **google)
        )
    after = await _revisions(pg_sessions)
    day = FIRST_DAY + timedelta(days=1)
    assert after[day] == before[day] + 1
    assert after[FIRST_DAY] == before[FIRST_DAY]


async def test_upgrade_from_previous_head_keeps_rows_and_drains(database_url, alembic_config):
    command.upgrade(alembic_config, "20261108_0027")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions.begin() as session:
            await register_range(session, FIRST_DAY, FIRST_DAY, TIMEZONE)
            await session.execute(
                text(
                    "INSERT INTO steps (id, google_data_point_id, start_time, end_time, count) "
                    "VALUES (:id, 'synthetic-steps', :start, :stop, 10)"
                ),
                {"id": uuid.uuid4(), "start": at(0), "stop": at(0) + timedelta(minutes=30)},
            )
        before = await _revisions(sessions)
        assert before[FIRST_DAY] == 2  # the 0027 triggers bumped it directly

        await engine.dispose()
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        async with sessions() as session:
            assert await verify_guards(session) == []
        assert await _revisions(sessions) == before

        async with sessions.begin() as session:
            session.add(steps_row(0))
        assert (await _revisions(sessions))[FIRST_DAY] == before[FIRST_DAY] + 1
        async with sessions() as session:
            assert (await session.execute(select(DerivedDayPending))).first() is None

        # A row committed here would mean invalidation did not run: downgrade refuses.
        async with sessions.begin() as session:
            await session.execute(
                text("ALTER TABLE derived_day_pending DISABLE TRIGGER derived_day_pending_drain")
            )
            await session.execute(
                text(
                    "INSERT INTO derived_day_pending (xact, day, timezone, method) "
                    "VALUES (1, :day, 'UTC', 'source-daily/6')"
                ),
                {"day": FIRST_DAY},
            )
        with pytest.raises(RuntimeError, match="holds 1 row"):
            command.downgrade(alembic_config, "20261108_0027")
        async with sessions.begin() as session:
            await session.execute(text("DELETE FROM derived_day_pending"))
            await session.execute(
                text(
                    "ALTER TABLE derived_day_pending "
                    "ENABLE ALWAYS TRIGGER derived_day_pending_drain"
                )
            )
        command.downgrade(alembic_config, "20261108_0027")
        assert await _revisions(sessions) == {FIRST_DAY: before[FIRST_DAY] + 1}
        async with sessions() as session:
            found = await session.scalar(text("SELECT to_regclass('derived_day_pending')"))
            assert found is None
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
    finally:
        await engine.dispose()
