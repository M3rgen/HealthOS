"""The synthetic fixtures the browser tests run on stay the shape of the real API.

``frontend/preview/fixtures.cjs`` is hand-written and every browser test answers the backend from
it. Here the real FastAPI routes run in-process over a synthetic archive and each response is
compared with the fixture served for that route, so a schema change breaks this test before the
browser tests silently keep passing on the old shape. Real fields the interface does not read are
named in ``NOT_IN_FIXTURE`` and proven unread; everything else must exist on both sides.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from support.api_shape import drift, merged, shape
from support.clock import freeze_app_clock
from support.garmin import unlocked
from test_dashboard_characterization import seed

from app.api import dashboard, devices, health, settings, sync, webhooks
from app.api import garmin as garmin_api
from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.db.session import get_session
from app.models.garmin import GarminRawRecord
from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.state import JobStatus, RawHealthData, SyncState
from app.runtime import build_runtime
from app.services.dashboard_views.dashboard_workouts import place_workouts

ROOT = Path(__file__).resolve().parents[1]
AT = datetime(2026, 9, 29, 12, tzinfo=UTC)
FIXTURES = ROOT / "frontend" / "preview" / "fixtures.cjs"
INTERFACE = ROOT / "frontend" / "src"

# fixtures.cjs export -> (the real request, the query the fixture function receives).
ROUTES = {
    "health": ("/health", None),
    "garminStatus": ("/api/garmin/status", None),
    "dashboard": (
        "/api/dashboard?end_date=2026-09-29&days=3",
        {"days": "3", "end_date": "2026-09-29"},
    ),
    "history": ("/api/dashboard/history?metric=steps", {"metric": "steps"}),
    "measurements": ("/api/dashboard/measurements", None),
    "syncStatus": ("/sync/status", None),
    "workouts": ("/api/dashboard/workouts?month=2026-09-01", {"month": "2026-09-01"}),
    "coverage": (
        "/api/sync/coverage?start_date=2026-09-28&end_date=2026-09-29&timezone=UTC&data_type=steps",
        {"start_date": "2026-09-28", "end_date": "2026-09-29", "data_type": "steps"},
    ),
    "webhookStatus": ("/api/webhooks/status", None),
    "devices": ("/api/devices", None),
    "appearanceSettings": ("/api/settings/appearance", None),
}

# Objects keyed by data, not by schema: metric names, device names, counter names.
MAPS: dict[str, tuple[str, ...]] = {
    "health": (".data_freshness",),
    "garminStatus": (".kinds",),
    "appearanceSettings": (".selection",),
    "webhookStatus": (".counters.reasons", ".counters.units", ".process_local.unpersisted"),
    "dashboard": (".profile",),
}

# Real fields no interface code reads, so no fixture has to carry them. A field read by the
# interface may not be listed here (test_unread_fields_are_really_unread).
NOT_IN_FIXTURE: dict[str, set[str]] = {
    "health": {
        ".data_freshness_checked_at",
        ".identity_lock_contention",
        ".identity_lock_contention[]",
        ".last_reconciliation",
        ".last_webhook_processed",
        ".oldest_frequent_sync",
        ".rejected_records",
        ".watchdog",
    },
    "dashboard": {
        ".profile.*.uncertainties",
        ".profile.*.uncertainties[]",
        ".days[].sleep_assumptions",
        ".days[].sleep_assumptions[]",
        ".sleep_method",
        ".charts.sources.heart_rate.activity_windows",
    },
    "coverage": {
        ".receipts_since",
        ".types[].source_regimes[].first_date",
        ".types[].source_regimes[].last_date",
        ".types[].source_regimes[].partial_today",
    },
    "webhookStatus": {
        ".counters.expired_basis",
        ".counters.semantics",
        ".counters.survives_restart",
        ".notification_basis",
        ".process_local",
    },
}
NOT_IN_FIXTURE_PREFIXES = {"webhookStatus": (".process_local.",)}


def read_fixtures() -> dict:
    """Evaluate fixtures.cjs with Node, the way the browser tests load it."""
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required: fixtures.cjs can only be read by evaluating it")
    program = """
        const fx = require(process.argv[1]);
        const out = {};
        const routes = JSON.parse(process.argv[2]);
        const q = (o) => new URLSearchParams(o || {});
        for (const [name, query] of Object.entries(routes)) {
          const value = fx[name];
          out[name] = typeof value === 'function' ? value(q(query)) : value;
        }
        out.scenarios = {};
        for (const scenario of ['kaynaklar', 'sync-error', 'empty']) {
          const overrides = fx.scenario(scenario);
          out.scenarios[scenario] = {};
          for (const [name, value] of Object.entries(overrides))
            if (routes[name] !== undefined) out.scenarios[scenario][name] =
              typeof value === 'function' ? value(q(routes[name])) : value;
        }
        console.log(JSON.stringify(out));
    """
    routes = {name: query for name, (_, query) in ROUTES.items()}
    result = subprocess.run(
        [node, "-e", program, str(FIXTURES), json.dumps(routes)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


async def real_responses(sessions, monkeypatch, tmp_path) -> dict[str, list]:
    """The real routes over a synthetic archive; a list because a route may need several samples."""
    config = Settings(
        _env_file=None,
        app_env="test",
        healthos_sources="google,garmin",
        backup_status_file=tmp_path / "absent",
        garmin_api_key_file=tmp_path / "absent-garmin-key",
    )
    runtime = build_runtime(config, sessions)
    runtime.webhook_status.started_at = AT
    runtime.discovery.devices = AsyncMock(
        return_value=[
            {"name": "Garmin", "device_type": "WATCH", "last_sync_time": "2026-09-29T11:00:00Z"}
        ]
    )
    app = FastAPI()
    app.state.settings, app.state.runtime = config, runtime
    app.state.write_limits = WriteLimits(config)
    for module in (dashboard, devices, garmin_api, health, settings, sync, webhooks):
        app.include_router(module.router)
    app.include_router(sync.coverage_router)
    app.openapi()  # builds response models from the real date type, before the clock is frozen
    freeze_app_clock(monkeypatch, AT)

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[runtime_dep] = lambda: runtime
    app.dependency_overrides[settings_dep] = lambda: config
    await seed(sessions, AT.date(), "UTC")
    runtime.garmin = unlocked(config, sessions)
    async with sessions.begin() as session:
        await session.execute(update(RawHealthData).values(updated_at=AT))
        session.add(
            GarminRawRecord(
                kind="steps",
                key="2026-09-29",
                body={"totalSteps": 1234},
                body_sha256="a" * 64,
                first_seen_at=AT,
            )
        )
        session.add(
            SyncState(
                data_type="steps",
                status=JobStatus.completed,
                records_downloaded=12,
                last_successful_sync=AT,
            )
        )
        session.add(
            ManualWorkout(
                id=UUID(int=11),
                exercise_type="Yürüyüş",
                start_time=AT,
                end_time=AT.replace(hour=13),
                created_at=AT,
                updated_at=AT,
            )
        )
    try:
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            real: dict[str, list] = {}
            for name, (path, _) in ROUTES.items():
                response = await client.get(path)
                assert response.status_code == 200, (path, response.text)
                real[name] = [response.json()]
            async with sessions.begin() as session:
                session.add_all(
                    ManualMeasurement(metric="weight", value=value, measured_at=AT)
                    for value in (70, 71)
                )
            conflict = await client.get(ROUTES["dashboard"][0])
            assert conflict.status_code == 200, conflict.text
            real["dashboard"].append(conflict.json())
            assert conflict.json()["profile"]["weight"]["value"] is None
            assert conflict.json()["profile"]["weight"]["uncertainties"] == ["conflicting_values"]
            flag = await client.post(
                f"/api/dashboard/measurements/{UUID(int=1)}/flag",
                json={"reason": "Sentetik kayıt"},
                headers={"X-HealthOS-Request": "1"},
            )
            assert flag.status_code in (200, 201), flag.text
            flagged = await client.get(ROUTES["measurements"][0])
            real["measurements"].append(flagged.json())
    finally:
        await runtime.shutdown()
    # Overlapping sessions from two sources need provider data the archive above does not hold;
    # the real placement code produces the same response field from two real-shaped rows.
    workout = real["workouts"][0][0]
    fitbit = workout | {"id": "f", "source_code": "FITBIT", "provider": "GOOGLE"}
    garmin = workout | {"id": "g", "source_code": "GARMIN", "provider": "GARMIN"}
    real["workouts"].append(place_workouts([fitbit, garmin]))
    return real


def expected_drift(name: str, samples: list, fixture_value) -> list[str]:
    maps = MAPS.get(name, ())
    real_shape = merged(*(shape(sample, maps=maps) for sample in samples))
    allowed = set(NOT_IN_FIXTURE.get(name, ()))
    prefixes = NOT_IN_FIXTURE_PREFIXES.get(name, ())
    if prefixes:
        allowed |= {path for path in real_shape if path.startswith(prefixes)}
    return drift(real_shape, shape(fixture_value, maps=maps), real_only=allowed)


async def test_each_fixture_has_the_shape_of_its_real_response(sessions, monkeypatch, tmp_path):
    real = await real_responses(sessions, monkeypatch, tmp_path)
    fixture = read_fixtures()
    assert {
        name: expected_drift(name, real[name], fixture[name]) for name in ROUTES
    } == dict.fromkeys(ROUTES, [])
    # The edge-case data sets may leave fields out; they may not invent one or change its type.
    for scenario, overrides in fixture["scenarios"].items():
        invented = {
            name: [
                item
                for item in expected_drift(name, real[name], value)
                if not item.startswith("fixture lacks")
            ]
            for name, value in overrides.items()
        }
        assert invented == dict.fromkeys(overrides, []), scenario


def test_unread_fields_are_really_unread():
    interface = "\n".join(p.read_text(encoding="utf-8") for p in sorted(INTERFACE.rglob("*.js")))
    named = {
        re.split(r"[.\[\]]+", path.strip(".[]"))[-1]
        for paths in NOT_IN_FIXTURE.values()
        for path in paths
    }
    read = sorted(key for key in named if re.search(rf"\b{key}\b", interface))
    assert read == [], "the interface reads these fields: add them to fixtures.cjs"


@pytest.mark.parametrize(
    ("real_value", "fixture_value", "message"),
    [
        ({"a": 1}, {"b": 1}, "fixture invents .b"),
        ({"a": 1, "b": 2}, {"a": 1}, "fixture lacks .b"),
        ({"a": "1.0"}, {"a": 1}, ".a: real API returns ['string'], fixture has ['number']"),
        ([{"a": 1}], [{"a": 1, "c": 2}], "fixture invents [].c"),
        ({"a": 1}, {"a": ["x"]}, ".a: real API returns ['number'], fixture has ['array']"),
    ],
)
def test_the_comparison_reports_each_kind_of_drift(real_value, fixture_value, message):
    assert message in drift(shape(real_value), shape(fixture_value))


def test_one_wrong_element_is_enough_to_be_drift():
    message = "[]: real API returns ['number'], fixture has ['number', 'string']"
    assert message in drift(shape([1, 2]), shape([1, "2"]))


def test_null_and_integer_float_are_not_drift():
    assert drift(shape({"a": None, "b": 1.5}), shape({"a": 3, "b": 2})) == []
    assert drift(shape({"a": 3}), shape({"a": None})) == []


def test_a_new_field_in_a_real_response_fails_until_someone_decides():
    fixture = {"days": [{"date": "2026-09-29", "steps": 1}]}
    changed = {"days": [{"date": "2026-09-29", "steps": 1, "steps_v2": 1}]}
    assert "fixture lacks .days[].steps_v2" in drift(shape(changed), shape(fixture))
