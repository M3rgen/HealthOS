"""The ordered time-choice pass decides exactly like the per-row search, in linear work."""

import random
from datetime import UTC, date, datetime, timedelta
from functools import partial

import pytest
from sqlalchemy import Select, case, func, insert, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.garmin import GarminDayTime, GarminRawRecord, GarminTimeEvidence
from app.services.providers.garmin_current import current_record_ids
from app.services.providers.garmin_time_state import time_choice_pending

START, SEEN = date(2020, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)


def reference_pending(current_heart_ids: Select):
    """Independent copy of the former correlated per-row nearest search (semantic reference)."""
    raw, choices, evidence = GarminRawRecord, GarminDayTime, GarminTimeEvidence
    current = (
        select(raw.key.label("day"), evidence.raw_record_id, evidence.day_ordinal)
        .join(evidence, evidence.raw_record_id == raw.id)
        .where(raw.id.in_(current_heart_ids), evidence.offset_minutes.is_not(None))
        .cte("reference_evidence")
    )
    latest = (
        select(func.max(choices.id)).where(choices.day == raw.key).correlate(raw).scalar_subquery()
    )
    completed = (
        select(choices.raw_record_id).where(choices.id == latest).correlate(raw).scalar_subquery()
    )
    ordinal = (
        select(choices.day_ordinal).where(choices.id == latest).correlate(raw).scalar_subquery()
    )
    before = (
        select(current.c.raw_record_id)
        .where(current.c.day <= raw.key)
        .order_by(current.c.day.desc())
        .limit(1)
        .correlate(raw)
    )
    after = (
        select(current.c.raw_record_id)
        .where(current.c.day > raw.key)
        .order_by(current.c.day)
        .limit(1)
        .correlate(raw)
    )
    left, right = before.scalar_subquery(), after.scalar_subquery()
    left_day = before.with_only_columns(current.c.day_ordinal).scalar_subquery()
    right_day = after.with_only_columns(current.c.day_ordinal).scalar_subquery()
    selected = case(
        (left.is_(None), right),
        (right.is_(None), left),
        (ordinal - left_day <= right_day - ordinal, left),
        else_=right,
    )
    return or_(latest.is_(None), completed.is_distinct_from(selected))


async def store(session, days: int, rng: random.Random | None, *, hearts=0.4, invalid=0.2) -> None:
    """Gappy days, superseded heart versions, invalid offsets and A -> B -> A choice histories.

    Without ``rng`` every day is complete: one valid heart version and its own last choice.
    """
    pick = rng or random.Random(0)
    raws: list[tuple[str, str]] = []
    for offset in range(days):
        key = str(START + timedelta(days=offset))
        raws += [("steps", key)] * (rng is None or pick.random() < 0.7)
        if rng is None or pick.random() < hearts:
            raws += [("heart-rate", key)] * (1 if rng is None else pick.choice((1, 1, 2)))
        raws += [("sleep", key)] * (rng is not None and pick.random() < 0.1)
    rows = [
        dict(kind=kind, key=key, body={}, body_sha256=f"{index:064x}", first_seen_at=SEEN)
        for index, (kind, key) in enumerate(raws)
    ]
    statement = insert(GarminRawRecord).returning(GarminRawRecord.id, sort_by_parameter_order=True)
    ids = list(await session.scalars(statement, rows))
    beats = [
        (i, date.fromisoformat(key))
        for i, (kind, key) in zip(ids, raws, strict=True)
        if kind == "heart-rate"
    ]
    evidence, choices = [], []
    for identifier, day in beats:
        if rng is None or pick.random() < 0.85:
            offset = (
                None if rng is not None and pick.random() < invalid else 60 * pick.randint(-5, 9)
            )
            evidence.append(
                dict(raw_record_id=identifier, day_ordinal=day.toordinal(), offset_minutes=offset)
            )
    for offset in range(days):
        day = START + timedelta(days=offset)
        pool = [None, *(i for i, other in beats if abs((other - day).days) <= 3)]
        history = [pick.choice(pool) for _ in range(pick.choice((0, 1, 1, 2, 3)))]
        if rng is None:
            history = [i for i, other in beats if other == day]
        choices += [
            dict(day=str(day), day_ordinal=day.toordinal(), raw_record_id=i) for i in history
        ]
    for table, values in ((GarminTimeEvidence, evidence), (GarminDayTime, choices)):
        if values:
            await session.execute(insert(table), values)


def pending(condition) -> Select:
    raw = GarminRawRecord
    return select(raw.key).where(condition(current_record_ids(raw.kind == "heart-rate"))).distinct()


# seed, days, heart share, invalid-offset share
CASES = [
    (1, 60, 0.4, 0.2),
    (2, 60, 0.15, 0.1),
    (3, 90, 0.8, 0.3),
    (4, 45, 0.4, 1.0),
    (5, 45, 0.0, 0.0),
    (6, 120, 0.3, 0.0),
    (7, 75, 0.5, 0.5),
    (8, 30, 0.9, 0.0),
]


@pytest.mark.parametrize(("seed", "days", "hearts", "invalid"), CASES)
async def test_ordered_pass_matches_per_row_reference(sessions, seed, days, hearts, invalid):
    async with sessions.begin() as session:
        await store(session, days, random.Random(seed), hearts=hearts, invalid=invalid)
    async with sessions() as session:
        expected = set(await session.scalars(pending(reference_pending)))
        actual = set(await session.scalars(pending(time_choice_pending)))
        keys = set(await session.scalars(select(GarminRawRecord.key).distinct()))
    assert actual == expected
    assert expected and keys - expected


async def test_steady_state_work_grows_linearly_with_archive_days(tmp_path):
    """SQLite VM steps, not wall time: the per-row search did 4x the work per doubling."""
    steps, ticks = {}, []
    for days in (1_000, 5_000):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'scale-{days}.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine).begin() as session:
            await store(session, days, None)
        async with engine.connect() as connection:
            driver = (await connection.get_raw_connection()).driver_connection
            await driver.set_progress_handler(partial(ticks.append, days), 100)
            assert not (await connection.execute(pending(time_choice_pending))).all()
            steps[days] = ticks.count(days)
        await engine.dispose()
    assert steps[1_000] > 0
    assert steps[5_000] <= 6 * steps[1_000]
