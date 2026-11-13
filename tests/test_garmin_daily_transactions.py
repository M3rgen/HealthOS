"""Completed daily outcomes survive rollback, bounded retries and process restarts."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_records import store_page
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_garmin_bounded import sessions as sessions
from test_garmin_normalize import DAY, NOW

from app.models.garmin import GarminDailyProjection
from app.models.health import Distance, Steps
from app.services.providers import garmin_bounded, garmin_normalize
from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows
from app.services.providers.garmin_readers import daily_values
from app.services.providers.garmin_versions import current_row


async def snapshot(sessions):
    async with sessions() as session:
        return {
            model: (await session.execute(select(model.__table__).order_by(model.id))).all()
            for model in (Steps, Distance, GarminDailyProjection)
        }


async def test_values_and_invalid_outcomes_publish_and_rollback_together(sessions):
    a = {"calendarDate": str(DAY), "totalSteps": 7000, "totalDistance": 5400}
    b = {**a, "totalSteps": None, "totalDistance": 6000}
    await store_page(sessions, "steps", [a], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    before = await snapshot(sessions)
    await store_page(sessions, "steps", [b], NOW + timedelta(minutes=1))
    assert await snapshot(sessions) == before
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
            assert await daily_values(session, DAY, DAY, ZoneInfo("UTC")) == {}
            assert await snapshot(sessions) == before  # Another connection still sees A.
            assert (await session.scalars(select(Distance.meters))).one() == 6000
            raise RuntimeError("synthetic rollback")
    assert await snapshot(sessions) == before
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    after = await snapshot(sessions)
    assert after[Steps] == before[Steps]
    assert len(after[GarminDailyProjection]) == 4
    async with sessions() as session:
        assert await daily_values(session, DAY, DAY, ZoneInfo("UTC")) == {}
    # A fresh engine represents a restarted process reading only persisted evidence.
    engine = create_async_engine(sessions.kw["bind"].url)
    try:
        restarted = async_sessionmaker(engine, expire_on_commit=False)
        assert await snapshot(restarted) == after
        async with restarted() as session:
            assert await daily_values(session, DAY, DAY, ZoneInfo("UTC")) == {}
    finally:
        await engine.dispose()


@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
async def test_bounded_rebuild_keeps_legacy_until_completed_and_resumes_failed_chunk(
    sessions, monkeypatch, version
):
    first = DAY - timedelta(days=15)
    days = [first + timedelta(days=index) for index in range(16)]
    for index, day in enumerate(days):
        a = {"calendarDate": str(day), "totalSteps": 7000, "totalDistance": 5400}
        async with sessions.begin() as session:
            rows, _ = build_rows({"steps": {str(day): a}}, day, day, "Garmin")
            # Simulate normalization performed before daily outcome evidence existed.
            for model, values in rows.items():
                if version == "v1" or (version == "mixed" and index % 2):
                    for row in values:
                        row["google_data_point_id"] = row["google_data_point_id"].removesuffix(
                            ":v2"
                        )
                await write_rows(session, model, values)
            await normalize_window(session, day, day, activities=False)
            assert (await daily_values(session, day, day, ZoneInfo("UTC")))[day]["steps"] == 7000
        await store_page(sessions, "steps", [a], NOW)
        await store_page(sessions, "steps", [{**a, "totalSteps": None}], NOW + timedelta(minutes=1))
    async with sessions() as session:
        assert len(await daily_values(session, first, DAY, ZoneInfo("UTC"))) == 16
        assert await session.scalar(select(func.count()).select_from(GarminDailyProjection)) == 0
    before = await snapshot(sessions)
    original = garmin_normalize.write_daily_projections

    async def fail_second_chunk(session, record_ids, rows):
        await original(session, record_ids, rows)
        if ("steps", str(days[7])) in record_ids:
            raise RuntimeError("synthetic chunk failure")

    monkeypatch.setattr(garmin_normalize, "write_daily_projections", fail_second_chunk)
    with pytest.raises(RuntimeError, match="synthetic chunk failure"):
        await garmin_bounded.normalize_bounded(sessions, first, DAY, device="Garmin")
    async with sessions() as session:
        visible = await daily_values(session, first, DAY, ZoneInfo("UTC"))
        assert set(visible) == set(days[7:])
        assert await session.scalar(select(func.count()).select_from(GarminDailyProjection)) == 14
    monkeypatch.setattr(garmin_normalize, "write_daily_projections", original)
    # Dispose all connections before retrying; change the chunk boundary as well.
    engine = create_async_engine(sessions.kw["bind"].url)
    try:
        restarted = async_sessionmaker(engine, expire_on_commit=False)
        later = DAY + timedelta(days=30)
        resumed = await garmin_bounded.normalize_bounded(
            restarted, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
        )
        assert resumed.updated == 0 and resumed.inserted + resumed.unchanged == 9  # distance only
        after = await snapshot(restarted)
        assert after[Steps] == before[Steps]
        assert len(after[GarminDailyProjection]) == 32
        for start, stop in garmin_bounded.day_chunks(first, DAY, days=3):
            async with restarted.begin() as session:
                await normalize_window(session, start, stop, activities=False)
        assert await snapshot(restarted) == after
        async with restarted() as session:
            assert await daily_values(session, first, DAY, ZoneInfo("UTC")) == {}
            assert (await session.scalars(select(Steps).where(current_row(Steps)))).all() == []
        a = {"calendarDate": str(first), "totalSteps": 7000, "totalDistance": 5400}
        await store_page(restarted, "steps", [a], NOW + timedelta(minutes=2))
        await garmin_bounded.normalize_bounded(
            restarted, later, later, device="Garmin", seen_since=NOW + timedelta(days=31)
        )
        async with restarted() as session:
            assert await daily_values(session, first, DAY, ZoneInfo("UTC")) == {
                first: {"steps": 7000}
            }
            original_ids = [row.id for row in before[Steps]]
            retained = (
                await session.execute(
                    select(Steps.__table__).where(Steps.id.in_(original_ids)).order_by(Steps.id)
                )
            ).all()
            assert retained == before[Steps]
    finally:
        await engine.dispose()
