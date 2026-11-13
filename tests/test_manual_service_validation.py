"""Direct service callers obey the same manual-entry rules before any database write."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dashboard_views.dashboard_workouts import create_workout
from app.services.readers.dashboard_manual import create_measurement

AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes",
    [
        {"end_time": AT},
        {"start_time": AT.replace(tzinfo=None)},
        {"end_time": datetime.max.replace(tzinfo=UTC)},
        {"exercise_type": "\x00"},
        {"exercise_type": " "},
        {"distance_meters": Decimal("-1")},
        {"calories_kcal": Decimal("NaN")},
        {"calories_kcal": Decimal("1E-20000")},
    ],
)
async def test_invalid_workout_service_input_never_reaches_persistence(changes):
    session = Mock(spec=AsyncSession)
    values = dict(exercise_type="Yürüyüş", start_time=AT, end_time=AT + timedelta(hours=1))
    with pytest.raises(ValueError):
        await create_workout(session, values | changes)
    session.add.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"metric": "unknown"},
        {"value": Decimal("0")},
        {"value": Decimal("501")},
        {"value": Decimal("Infinity")},
        {"value": Decimal("1E-20000")},
        {"measured_at": AT.replace(tzinfo=None)},
        {"measured_at": datetime.max.replace(tzinfo=UTC)},
    ],
)
async def test_invalid_measurement_service_input_never_reaches_persistence(changes):
    session = Mock(spec=AsyncSession)
    values = dict(metric="weight", value=Decimal("70"), measured_at=AT)
    with pytest.raises(ValueError):
        await create_measurement(session, values | changes)
    session.add.assert_not_called()
    session.flush.assert_not_called()
