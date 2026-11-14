"""First prepared sample wins a Garmin heart key, including corrections and replays."""

from datetime import timedelta

import garmin_records
import pytest
from sqlalchemy import func, select, update
from test_garmin_normalize import DAY, NOW, garmin_heart

from app.models.garmin import (
    GarminApiResponse,
    GarminHeartMember,
    GarminHeartSet,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import HeartRate
from app.services.providers.garmin_normalize import build_rows, normalize_window


@pytest.mark.parametrize(
    "values, expected, skipped",
    [
        ([60, 60], 60, 0),
        ([60, 80], 60, 0),
        ([80, 60], 80, 0),
        ([-1, 60], -1, 0),
        ([-0.0001, 60], 0, 0),
        (["-1e-10000", 60], 0, 0),
        ([60, -1], 60, 0),
        (["-1e10000", 60], 60, 1),
    ]
    + [([bad, 60, 80], 60, int(bad == 10**30)) for bad in (None, "bad", "NaN", 10**30)],
)
@pytest.mark.parametrize("correction", [False, True])
async def test_first_prepared_sample_is_stable_with_unique_counts(
    sessions, values, expected, skipped, correction
):
    if correction:
        await garmin_records.store_page(
            sessions, "heart-rate", [garmin_heart(DAY, [(0, 90), (1, 70)])], NOW
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
            await session.execute(
                update(HeartRate)
                .where(HeartRate.bpm == 90)
                .values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
            )
    body = garmin_heart(DAY, [(0, value) for value in values])
    rows, rejected = build_rows({"heart-rate": {str(DAY): body}}, DAY, DAY, "Garmin")
    assert rejected == skipped
    assert [row["bpm"] for row in rows[HeartRate]] == [expected]
    await garmin_records.store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=1))
    evidence = (GarminApiResponse, GarminRawRecord, GarminRecordObservation)
    evidence += (GarminHeartSet, GarminHeartMember, HeartRate)
    async with sessions() as session:
        archived = [
            (await session.execute(select(model.__table__))).all() for model in evidence[:3]
        ]
    baseline = None
    for run in range(4):
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
        assert stats.skipped == skipped
        assert stats.tables["heart_rate"] == {
            "inserted": int(run == 0 and not correction),
            "updated": int(run == 0 and correction),
            "unchanged": int(run > 0),
        }
        async with sessions() as session:
            saved = [(await session.execute(select(model.__table__))).all() for model in evidence]
            assert saved[:3] == archived
            row = (await session.scalars(select(HeartRate).order_by(HeartRate.timestamp))).first()
            assert row.bpm == expected and row.is_quarantined == correction
            assert row.quarantine_reason == ("Synthetic exclusion" if correction else None)
            latest_set = await session.scalar(select(func.max(GarminHeartSet.id)))
            members = (
                await session.scalars(
                    select(GarminHeartMember.sample_key).where(
                        GarminHeartMember.set_id == latest_set
                    )
                )
            ).all()
            assert members == [row.google_data_point_id]
        if baseline is not None:
            assert saved == baseline  # Includes updated_at and the entire membership evidence.
            assert stats.inserted == stats.updated == 0
        baseline = saved
