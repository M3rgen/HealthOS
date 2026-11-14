"""Mixed-provider JSON digests captured from deliberately synthetic provider inputs.

After an intentional fixture change, regenerate with
``pytest -q --tb=short --showlocals tests/test_provider_characterization.py``:
capture each case's computed ``digest`` into EXPECTED, then rerun without updating.
The JSON still comes from the real readers; never substitute hand-edited output.
"""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from derived_records import snapshot
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from starlette.responses import JSONResponse
from support.clock import freeze_app_clock
from test_dashboard_characterization import seed
from test_garmin_normalize import DAY, archive_day

from app.models.health import ExerciseSession
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.identities.source_precedence import Policy
from app.services.providers.garmin_normalize import normalize_window
from app.services.readers.dashboard_queries import DashboardQuery

# Platform candidates and selected hourly evidence intentionally change these digests.
EXPECTED = {
    ("UTC", False, "live"): "d61e0fd1a3a9b342ea1ff7a0d16e70100528e57e267c8cfea05e1af37ad4f985",
    (
        "Europe/Paris",
        False,
        "live",
    ): "d25ac5f34b1b352e2e61e2c22d22de18b5d7d8e38b54e5516bed8542e6affaae",
    (
        "America/New_York",
        False,
        "live",
    ): "56e85c43a3369e26c9fb8dbec2dc4ad0b3f8592bc9c6122e07f26163ca23d3b0",
    ("UTC", True, "live"): "d3dac45e6bd1ac0bbcff58e9c226d488cf569bcce37466d5b45b04b16313bedc",
    (
        "Europe/Paris",
        True,
        "live",
    ): "7c55a60142f0cb37b4e30be8fb40c9b992b439c82fcc057a333c288399784958",
    (
        "America/New_York",
        True,
        "live",
    ): "940cdcc07d10241f6ff2c0565f1da298f7595cb04ec2997c68c720a99c4189a2",
    ("UTC", False, "deleted"): "b9c45e8ea05c78b3f78a7192c7fc018ecd69e0ee7e5e2fb1edfd369d85555f9c",
    (
        "Europe/Paris",
        False,
        "deleted",
    ): "52cbec2446f69057c4b9cda3211cc30ff9bc273a943f41bac75dbca23582de09",
    (
        "America/New_York",
        False,
        "deleted",
    ): "98612b65cf4bad971a836144c757115bec65f144997ebdb8817171cc532cacdc",
    ("UTC", True, "deleted"): "141bb62ba76faa684ab0f4a594b1514b1ea879d8a2ae4741a21eb8cd4f198099",
    (
        "Europe/Paris",
        True,
        "deleted",
    ): "d000b1b360b06e19c78764e58497f17f36efc4406bba76893648a57b27d7e787",
    (
        "America/New_York",
        True,
        "deleted",
    ): "e5eb714016bf1d05f594fc8b40262a82de73a8dad9a3123a1c8331c56e283326",
    ("UTC", False, "valid"): "d61e0fd1a3a9b342ea1ff7a0d16e70100528e57e267c8cfea05e1af37ad4f985",
    (
        "Europe/Paris",
        False,
        "valid",
    ): "d25ac5f34b1b352e2e61e2c22d22de18b5d7d8e38b54e5516bed8542e6affaae",
    (
        "America/New_York",
        False,
        "valid",
    ): "56e85c43a3369e26c9fb8dbec2dc4ad0b3f8592bc9c6122e07f26163ca23d3b0",
    ("UTC", True, "valid"): "d3dac45e6bd1ac0bbcff58e9c226d488cf569bcce37466d5b45b04b16313bedc",
    (
        "Europe/Paris",
        True,
        "valid",
    ): "7c55a60142f0cb37b4e30be8fb40c9b992b439c82fcc057a333c288399784958",
    (
        "America/New_York",
        True,
        "valid",
    ): "940cdcc07d10241f6ff2c0565f1da298f7595cb04ec2997c68c720a99c4189a2",
}


@pytest.mark.parametrize("zone", ["UTC", "Europe/Paris", "America/New_York"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mode", ["live", "deleted", "valid"])
async def test_mixed_provider_json_stays_identical(sessions, monkeypatch, zone, reverse, mode):
    freeze_app_clock(monkeypatch, datetime(2026, 11, 7, 12, tzinfo=UTC))
    await seed(sessions, DAY, zone)
    await archive_day(sessions)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
        exercise = (await session.scalars(select(ExerciseSession))).one()
        exercise.id = UUID(int=100)
    policy = (
        Policy(activity=("FITBIT", "GARMIN"), health=("GARMIN", "FITBIT")) if reverse else Policy()
    )
    async with sessions() as session:
        before = await snapshot(session)
        data = await dashboard_data(
            session,
            policy=policy,
            include_source_deleted=mode != "live",
            valid_sources_only=mode == "valid",
            query=DashboardQuery(DAY, 3, zone),
        )
        workouts = await list_workouts(session, DAY, ZoneInfo(zone))
        body = JSONResponse(jsonable_encoder({"dashboard": data, "workouts": workouts})).body
        digest = sha256(body).hexdigest()
        assert await snapshot(session) == before
        assert digest == EXPECTED[zone, reverse, mode]
