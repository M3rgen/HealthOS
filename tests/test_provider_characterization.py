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

EXPECTED = {
    ("UTC", False, "live"): "4a86c344c27afa223ebb4f491adbe6e8a3b3ddddfa4012820a16f67d4052fb72",
    (
        "Europe/Paris",
        False,
        "live",
    ): "b2c5641816dd226f4dd56fd3d7f273fd851b881d02bb0640ee033340f8e8b436",
    (
        "America/New_York",
        False,
        "live",
    ): "13848dcde9bdb97db8139c46149a856ecf6d0cd8f0807a1c3d24a15f87de522e",
    ("UTC", True, "live"): "7e9b3c0c5cf25ec9d5f34ba63be46d25ba5a386cd584e051ff40bcfefa3e0aea",
    (
        "Europe/Paris",
        True,
        "live",
    ): "242240373ca4b3c30da67d5481a6d2871acbceacbf9dde7e6633644af265e15a",
    (
        "America/New_York",
        True,
        "live",
    ): "da41ebe21187126168c04673bd741da5b8d9e096e352bbf6fae6d7d83996ee82",
    ("UTC", False, "deleted"): "47bc65938056ab7b9652dd35674697924d0ab823f8be14c7a97ad6653c925967",
    (
        "Europe/Paris",
        False,
        "deleted",
    ): "c9cd88bc1ca576ece4edbecfa151968c908f14b84d20fd7fc50075bac7c0a19b",
    (
        "America/New_York",
        False,
        "deleted",
    ): "fe973665005e3df672b98d8b9c8768af84e1cce97ff6c72e546f96a52b15b914",
    ("UTC", True, "deleted"): "75ece28ea0f2682bd9b5552600ffa12bc78c282e6cc985a935c8ccc523140c14",
    (
        "Europe/Paris",
        True,
        "deleted",
    ): "514a21186aa3fc6b93c15f02401a709764cfd0e573116448a5fbca421b6c2098",
    (
        "America/New_York",
        True,
        "deleted",
    ): "56b2a16a27735d66cf3e33d72c9752f39acf3ce667dff7d50fc4d8eb1331c211",
    ("UTC", False, "valid"): "4a86c344c27afa223ebb4f491adbe6e8a3b3ddddfa4012820a16f67d4052fb72",
    (
        "Europe/Paris",
        False,
        "valid",
    ): "b2c5641816dd226f4dd56fd3d7f273fd851b881d02bb0640ee033340f8e8b436",
    (
        "America/New_York",
        False,
        "valid",
    ): "13848dcde9bdb97db8139c46149a856ecf6d0cd8f0807a1c3d24a15f87de522e",
    ("UTC", True, "valid"): "7e9b3c0c5cf25ec9d5f34ba63be46d25ba5a386cd584e051ff40bcfefa3e0aea",
    (
        "Europe/Paris",
        True,
        "valid",
    ): "242240373ca4b3c30da67d5481a6d2871acbceacbf9dde7e6633644af265e15a",
    (
        "America/New_York",
        True,
        "valid",
    ): "da41ebe21187126168c04673bd741da5b8d9e096e352bbf6fae6d7d83996ee82",
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
