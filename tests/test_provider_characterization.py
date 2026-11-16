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
    ("UTC", False, "live"): "bd44ad79d2d70c2c98dc954498140d13776ae88e2e27fb9c13e42454ea4c11dd",
    (
        "Europe/Paris",
        False,
        "live",
    ): "0124abc0fa75d7e7ccae5d003e382bb1a9b16e9b4587835e15483dc6ef61d333",
    (
        "America/New_York",
        False,
        "live",
    ): "8c5bd086de182fcb18d6cd509d127cddfa3d0d85543f88d62cce85910e984988",
    ("UTC", True, "live"): "49573af5ad9fe1422dfa8607d6584d83f3a946495a3c993a370961bf802a4a48",
    (
        "Europe/Paris",
        True,
        "live",
    ): "aaf05b6322842a47e532e56e63aa4d65d5ba58148442d217f62bd776dcaff09d",
    (
        "America/New_York",
        True,
        "live",
    ): "20f0572c2ce9d59ba60e2963c732cf4ac4f5d1364bafd8b89687f0b993431311",
    ("UTC", False, "deleted"): "fb0e1f29948688a176651e4ded5be9a8c3070c287a8e5d673e68291e0a2601fa",
    (
        "Europe/Paris",
        False,
        "deleted",
    ): "707834a1c1d6d1652dc2bfab2da3329349c3be373b9e3824b82986b1cca76563",
    (
        "America/New_York",
        False,
        "deleted",
    ): "d6a963fbc4693df1790fbf36eef090d46f9481efafe3316fa04af5b7dc76dedd",
    ("UTC", True, "deleted"): "27c496c266d60b7e4aa84aa46819852af95ea69916f841bf02330c290ac5a520",
    (
        "Europe/Paris",
        True,
        "deleted",
    ): "d2fb296537f82d7097ad6207a2f966b43ec1580b5ce84dd0ab90d226cf156372",
    (
        "America/New_York",
        True,
        "deleted",
    ): "749f5f881a6a5c6ca157611c4d1ca2e565ffb9bfdf2c2e2c484ae5bb18644f4c",
    ("UTC", False, "valid"): "bd44ad79d2d70c2c98dc954498140d13776ae88e2e27fb9c13e42454ea4c11dd",
    (
        "Europe/Paris",
        False,
        "valid",
    ): "0124abc0fa75d7e7ccae5d003e382bb1a9b16e9b4587835e15483dc6ef61d333",
    (
        "America/New_York",
        False,
        "valid",
    ): "8c5bd086de182fcb18d6cd509d127cddfa3d0d85543f88d62cce85910e984988",
    ("UTC", True, "valid"): "49573af5ad9fe1422dfa8607d6584d83f3a946495a3c993a370961bf802a4a48",
    (
        "Europe/Paris",
        True,
        "valid",
    ): "aaf05b6322842a47e532e56e63aa4d65d5ba58148442d217f62bd776dcaff09d",
    (
        "America/New_York",
        True,
        "valid",
    ): "20f0572c2ce9d59ba60e2963c732cf4ac4f5d1364bafd8b89687f0b993431311",
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
