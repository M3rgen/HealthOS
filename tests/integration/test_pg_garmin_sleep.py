"""Real PostgreSQL Numeric rounding must not erase Garmin's source sign."""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from garmin_records import store_page
from sqlalchemy import select
from test_garmin_normalize import DAY, NOW, garmin_sleep

from app.models.health import SleepSession
from app.services.providers.garmin_normalize import normalize_window
from app.services.providers.garmin_readers import daily_values

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "field,column",
    [("sleepTimeSeconds", "duration_seconds"), ("awakeSleepSeconds", "awake_seconds")],
)
@pytest.mark.parametrize("value", [-0.0001, "-0.0001", "-1e-1000", "-0e-1000", "0.0001"])
async def test_garmin_sleep_sign_survives_real_numeric_rounding(pg_sessions, field, column, value):
    for index, payload in enumerate(
        (garmin_sleep(DAY), {**garmin_sleep(DAY), field: value}, garmin_sleep(DAY))
    ):
        await store_page(pg_sessions, "sleep", [payload], NOW + timedelta(minutes=index))
        async with pg_sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with pg_sessions() as session:
            values = await daily_values(
                session, DAY, DAY, ZoneInfo("UTC"), metrics=("sleep_seconds",)
            )
            if index == 1:
                assert (
                    await session.scalar(
                        select(getattr(SleepSession, column)).order_by(SleepSession.id.desc())
                    )
                    == 0
                )
                if Decimal(str(value)) < 0:
                    assert values == {}
                else:
                    assert values[DAY]["sleep_seconds"] == (
                        0 if field == "sleepTimeSeconds" else 28800
                    )
            else:
                assert values[DAY]["sleep_seconds"] == 27600
