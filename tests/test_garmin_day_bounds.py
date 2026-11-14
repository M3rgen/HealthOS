"""Unrepresentable daily ends preserve measurements and transactional evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from garmin_records import daily_steps, heart_rate, store_page
from sqlalchemy import select, update
from test_garmin_normalize import DAY, MS, NOW

from app.core.timeutil import as_utc
from app.models import garmin as g
from app.models.health import Distance, HeartRate, RestingHeartRate, Steps
from app.services.providers import garmin_bounded, garmin_normalize

VALUES = (Steps, Distance, HeartRate, RestingHeartRate)
EVIDENCE = (g.GarminDailyProjection, g.GarminHeartSet, g.GarminHeartMember)
ARCHIVE = (g.GarminApiResponse, g.GarminRawRecord, g.GarminRecordObservation)


async def snapshot(sessions, models):
    async with sessions() as session:
        saved = []
        for model in models:
            table = model.__table__
            saved.append((await session.execute(select(table).order_by(*table.primary_key))).all())
        return saved


@pytest.mark.parametrize("bounded", [False, True])
async def test_overflow_day_preserves_values_and_evidence(sessions, monkeypatch, bounded):
    days = [DAY + timedelta(days=i) for i in range(8)]
    bodies = [
        {**heart_rate(day), "heartRateValues": [[MS + i * 86_400_000, 60]]}
        for i, day in enumerate(days)
    ]
    await store_page(sessions, "heart-rate", bodies[:1], NOW)
    async with sessions.begin() as session:
        await garmin_normalize.normalize_window(session, DAY, DAY)
        await session.execute(
            update(HeartRate).values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
    bodies[0] = {**bodies[0], "endTimestampGMT": "9999-12-31T23:59:59Z"}
    for body in bodies:
        await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=1))
    await store_page(sessions, "steps", [daily_steps(day) for day in days], NOW)
    archived = await snapshot(sessions, ARCHIVE)
    before = await snapshot(sessions, VALUES + EVIDENCE)

    async def normalize():
        if bounded:
            return await garmin_bounded.normalize_bounded(sessions, DAY, days[-1], device="Garmin")
        async with sessions.begin() as session:
            return await garmin_normalize.normalize_window(session, DAY, days[-1])

    original = garmin_normalize.write_daily_projections

    async def fail_after_evidence(session, record_ids, rows, **kwargs):
        await original(session, record_ids, rows, **kwargs)
        raise RuntimeError("synthetic rollback after daily evidence")

    monkeypatch.setattr(garmin_normalize, "write_daily_projections", fail_after_evidence)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        await normalize()
    assert await snapshot(sessions, VALUES + EVIDENCE) == before
    monkeypatch.setattr(garmin_normalize, "write_daily_projections", original)
    stats = await normalize()
    assert stats.skipped == stats.updated == 0 and stats.inserted == 30
    async with sessions() as session:
        for model, field, expected in (
            (Steps, "count", daily_steps(DAY)["totalSteps"]),
            (Distance, "meters", daily_steps(DAY)["totalDistance"]),
            (HeartRate, "bpm", bodies[0]["heartRateValues"][0][1]),
            (RestingHeartRate, "bpm", heart_rate(DAY)["restingHeartRate"]),
        ):
            rows = (await session.scalars(select(model).order_by(model.source_record_id))).all()
            assert len(rows) == 8 and {getattr(r, field) for r in rows} == {expected}
            if model in (Steps, Distance):
                assert as_utc(rows[0].start_time) == datetime(2026, 11, 6, tzinfo=UTC)
                assert as_utc(rows[0].end_time) == datetime(2026, 11, 7, tzinfo=UTC)
        sample = (
            await session.scalars(select(HeartRate).order_by(HeartRate.source_record_id))
        ).first()
        assert sample.is_quarantined and sample.quarantine_reason == "Synthetic exclusion"
        projections = (await session.scalars(select(g.GarminDailyProjection))).all()
        assert len(projections) == 25 and all(p.normalized_key for p in projections)
        assert len((await session.scalars(select(g.GarminHeartSet))).all()) == 9
        assert len((await session.scalars(select(g.GarminHeartMember))).all()) == 9
    after = await snapshot(sessions, VALUES + EVIDENCE)
    again = await normalize()
    assert again.inserted == again.updated == 0 and again.unchanged == 32
    assert await snapshot(sessions, VALUES + EVIDENCE) == after
    assert await snapshot(sessions, ARCHIVE) == archived
