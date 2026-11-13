"""Activity values and conversion results publish or roll back together."""

from datetime import timedelta

import pytest
from garmin_records import activity, store_page
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_garmin_activity_results import START, STOP, normalize, snapshot
from test_garmin_bounded import sessions as sessions
from test_garmin_normalize import DAY, NOW

from app.models.garmin import GarminActivityResult
from app.models.health import ExerciseSession
from app.services.providers import garmin_bounded, garmin_normalize, garmin_readers


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("restore", [False, True])
async def test_archiving_and_rolled_back_results_preserve_completed_view(
    sessions, monkeypatch, mode, restore
):
    a = activity(1, DAY)
    b = {**a, "duration": 0}
    await store_page(sessions, "activities", [a], NOW)
    await normalize(sessions, mode)
    if restore:
        await store_page(sessions, "activities", [b], NOW + timedelta(minutes=1))
        await normalize(sessions, mode)
    async with sessions() as session:
        old_view = await garmin_readers.workouts(session, START, STOP)
    before = await snapshot(sessions, (ExerciseSession, GarminActivityResult))
    incoming = {**a, "calories": 300} if restore else b
    await store_page(sessions, "activities", [incoming], NOW + timedelta(minutes=2))
    engine = create_async_engine(sessions.kw["bind"].url)
    observer = async_sessionmaker(engine, expire_on_commit=False)
    module = garmin_normalize if mode == "direct" else garmin_bounded
    original = module.write_activity_results

    async def interrupted(session, record_ids, results):
        await original(session, record_ids, results)
        # This transaction has its new result; an independent connection still sees old values.
        visible = await garmin_readers.workouts(session, START, STOP)
        assert bool(visible) == restore
        assert await snapshot(observer, (ExerciseSession, GarminActivityResult)) == before
        async with observer() as reader:
            assert await garmin_readers.workouts(reader, START, STOP) == old_view
        raise RuntimeError("synthetic interruption after result publication")

    try:
        async with observer() as reader:
            assert await garmin_readers.workouts(reader, START, STOP) == old_view
        monkeypatch.setattr(module, "write_activity_results", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            await normalize(sessions, mode)
        assert await snapshot(observer, (ExerciseSession, GarminActivityResult)) == before
        async with observer() as reader:
            assert await garmin_readers.workouts(reader, START, STOP) == old_view
        monkeypatch.setattr(module, "write_activity_results", original)
        # Retry from a new engine against the same archive, without fetching again.
        await normalize(observer, mode)
        async with observer() as reader:
            visible = await garmin_readers.workouts(reader, START, STOP)
            assert bool(visible) == restore
            if restore:
                assert visible[0]["calories_kcal"] == incoming["calories"]
        completed = await snapshot(observer, (ExerciseSession, GarminActivityResult))
        await normalize(observer, mode)
        assert await snapshot(observer, (ExerciseSession, GarminActivityResult)) == completed
    finally:
        await engine.dispose()
