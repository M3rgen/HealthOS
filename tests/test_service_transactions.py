"""Caller rollback/commit includes all manual and preference writes as one transaction."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.manual_flags import ManualMeasurementFlagEvent
from app.models.settings import AppearanceSettings, DisplayPreferences
from app.services.dashboard_views.dashboard_workouts import create_workout
from app.services.operations.settings import (
    AppearanceSelection,
    DisplayPreferencesBody,
    put_appearance,
    put_display,
)
from app.services.readers.dashboard_manual import create_measurement, set_measurement_flag


async def write_all(session):
    now = datetime(2026, 9, 1, tzinfo=UTC)
    measurement = await create_measurement(
        session, dict(metric="weight", value=Decimal("70.5"), measured_at=now)
    )
    await set_measurement_flag(session, UUID(measurement["id"]), "test", True, "synthetic")
    await create_workout(
        session, dict(exercise_type="Yürüyüş", start_time=now, end_time=now + timedelta(hours=1))
    )
    await put_appearance(session, AppearanceSelection(logo="example"))
    await put_display(
        session, DisplayPreferencesBody(schema_version=1, preferences={"motion": False})
    )


MODELS = (
    ManualMeasurement,
    ManualMeasurementFlagEvent,
    ManualWorkout,
    AppearanceSettings,
    DisplayPreferences,
)


@pytest.mark.parametrize("commit", [False, True])
async def test_caller_owns_atomic_commit_or_rollback(sessions, commit):
    async with sessions() as session:
        await write_all(session)
        assert session.in_transaction()
        if commit:
            await session.commit()
        else:
            await session.rollback()
    async with sessions() as session:
        assert [
            await session.scalar(select(func.count()).select_from(model)) for model in MODELS
        ] == [int(commit)] * len(MODELS)


@pytest.mark.parametrize("existing", [False, True])
async def test_preference_first_and_subsequent_writes_can_be_rolled_back_alone(sessions, existing):
    if existing:
        async with sessions.begin() as session:
            await put_appearance(session, AppearanceSelection(logo="original"))
    async with sessions() as session:
        # Load the singleton first, like put_display's schema check does.
        row = await session.get(AppearanceSettings, 1)
        await put_appearance(session, AppearanceSelection(logo="temporary"))
        if existing:
            assert row.selection == {"logo": "temporary"}
        await session.rollback()
    async with sessions() as session:
        row = await session.get(AppearanceSettings, 1)
        assert (row.selection if row else None) == ({"logo": "original"} if existing else None)
