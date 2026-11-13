"""Real Garmin writes shared by simulated and PostgreSQL ownership-loss regressions."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from garmin_records import FakeGarmin, activity, daily_steps, store_page
from sqlalchemy import select

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.db.contracts import OwnershipLost
from app.garmin.client import REDACTED_BODY, GarminClient
from app.models.garmin import (
    GarminActivityResult,
    GarminApiResponse,
    GarminDailyProjection,
    GarminNormalizationState,
    GarminPullState,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import ExerciseSession, Steps
from app.services.providers import garmin_bounded, garmin_ingestion
from app.services.providers.garmin_pull import OWNERSHIP_LOST, GarminPull
from app.services.providers.garmin_versions import current_row
from app.sync.ownership import CheckedSessions

FIRST = date(2026, 1, 1)
LAST = FIRST + timedelta(days=28)
NOW = datetime(2026, 2, 1, tzinfo=UTC)


async def rows(sessions, *models):
    async with sessions() as session:
        return [
            (await session.execute(select(model.__table__).order_by(model.id))).all()
            for model in models
        ]


def client_at(tmp_path, transport):
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-key")
    return GarminClient(key, http=httpx.AsyncClient(transport=transport))


async def raw_loss(sessions, tmp_path, monkeypatch, lane, kind, owner, lose):
    monkeypatch.setattr(garmin_ingestion, "ACTIVITY_PAGE", 1)
    fake = FakeGarmin()
    bodies = (
        ([daily_steps(FIRST)], [daily_steps(LAST)])
        if kind == "steps"
        else ([activity(1, FIRST)], [activity(2, FIRST)])
    )
    path = garmin_ingestion.BY_NAME[kind].request(FIRST, LAST - timedelta(days=1))[0]
    if kind == "steps":
        fake.bodies[path] = bodies[0]
        fake.bodies[garmin_ingestion.BY_NAME[kind].request(LAST, LAST)[0]] = bodies[1]
    else:
        fake.bodies[path] = lambda r: (
            bodies[int(r.url.params["start"])] if int(r.url.params["start"]) < 2 else []
        )
    calls = 0

    async def transport(request):
        nonlocal calls
        if request.method == "GET":
            calls += 1
            if calls == 2:
                await lose(kind)  # The HTTP response has not returned to GarminSource yet.
        return fake(request)

    client = client_at(tmp_path, httpx.MockTransport(transport))
    runner = GarminPull(
        Settings(_env_file=None, garmin_lookback_days=28),
        sessions,
        client=client,
        clock=lambda: datetime.combine(LAST, datetime.min.time(), UTC),
    ).owned_by(owner)
    async with sessions.begin() as session:
        session.add(GarminPullState(kind=kind, last_success_at=NOW))
    try:
        if lane == "pull":
            report = await runner.run([kind])
            assert report.kinds[kind].error == OWNERSHIP_LOST
        else:
            report = await runner.backfill(FIRST, LAST, kinds=[kind])
            assert report["kinds"][kind]["error"] == OWNERSHIP_LOST
        async with sessions() as session:
            raw, observations, responses = await rows(
                sessions, GarminRawRecord, GarminRecordObservation, GarminApiResponse
            )
            assert len(raw) == len(observations) == 1 and raw[0].body == bodies[0][0]
            assert len(responses) == 3  # Token and each page exactly once.
            assert responses[0].body == REDACTED_BODY
            assert not any(o.response_id == responses[-1].id for o in observations)
            state = await session.get(GarminPullState, kind)
            assert as_utc(state.last_success_at) == NOW
            assert state.last_error_at is None and state.last_attempt_at is None
            if lane == "backfill":
                assert state.backfill_next == (LAST if kind == "steps" else FIRST)
        if lane == "backfill":
            recovered = await runner.backfill(FIRST, LAST, kinds=[kind])
            assert not recovered["failed"] and recovered["kinds"][kind]["complete"]
        else:
            assert not (await runner.run([kind])).failed
        assert len((await rows(sessions, GarminRawRecord))[0]) == 2
    finally:
        await runner.aclose()


async def normalization_loss(sessions, tmp_path, monkeypatch, lane, kind, owner, lose):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 1)
    days = (FIRST, FIRST + timedelta(days=7))
    payloads = (
        [daily_steps(d, steps=100) for d in days]
        if kind == "steps"
        else [activity(i, FIRST) for i in (1, 2)]
    )
    await store_page(sessions, kind, payloads, NOW)
    client = client_at(tmp_path, FakeGarmin().transport())
    runner = GarminPull(
        Settings(_env_file=None, garmin_lookback_days=7),
        sessions,
        client=client,
        clock=lambda: datetime.combine(days[-1], datetime.min.time(), UTC),
    ).owned_by(owner)
    await runner.normalize(*days)
    model, result = (
        (Steps, GarminDailyProjection)
        if kind == "steps"
        else (ExerciseSession, GarminActivityResult)
    )
    before = await rows(sessions, model, result)
    await store_page(
        sessions,
        kind,
        [{**payload, "totalSteps": 200, "calories": 300} for payload in payloads],
        NOW + timedelta(seconds=1),
    )
    name = "normalize_window" if kind == "steps" else "normalize_activities_page"
    original = getattr(garmin_bounded, name)
    count = 0

    async def convert(*args, **kwargs):
        nonlocal count
        count += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(garmin_bounded, name, convert)

    # Called before begin: the previous chunk has committed, the next has not started.
    async def boundary():
        nonlocal count
        if count == 1:
            count += 1
            await lose("normalize")

    async def invoke():
        if lane == "normalize":
            return await runner.normalize(*days)
        if lane == "pull":
            return await runner.run(["hrv"])
        return await runner.backfill(*days, kinds=["hrv"])

    return runner, boundary, invoke, before, model, result


async def assert_partial_normalization(
    sessions, runner, invoke, before, model, result, *, failure=OwnershipLost
):
    state_before = await rows(sessions, GarminNormalizationState)
    with pytest.raises(failure):
        await invoke()
    assert await rows(sessions, GarminNormalizationState) == state_before
    partial = await rows(sessions, model, result)
    assert len(partial[1]) == len(before[1]) + (2 if model is Steps else 1)
    assert {row.id for row in before[0]} <= {row.id for row in partial[0]}
    assert all(row in partial[1] for row in before[1])
    async with sessions() as session:
        visible = (
            await session.scalars(
                select(model).where(current_row(model)).order_by(model.source_record_id)
            )
        ).all()
        assert [r.count if model is Steps else r.calories_kcal for r in visible] == (
            [200, 100] if model is Steps else [300, activity(1, FIRST)["calories"]]
        )
    await runner.normalize(FIRST, FIRST + timedelta(days=7))
    completed = await rows(sessions, model, result)
    assert len(completed[1]) == len(before[1]) + (4 if model is Steps else 2)
    await runner.normalize(FIRST, FIRST + timedelta(days=7))
    assert await rows(sessions, model, result) == completed


def at_boundaries(owner, boundary):
    @asynccontextmanager
    async def observed(key):
        async with owner(key) as held:
            yield CheckedSessions(held, boundary) if key == "normalize" else held

    return observed
