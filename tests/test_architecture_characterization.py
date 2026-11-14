"""Synthetic contracts captured before reducing the issue #320 function budgets."""

from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from derived_records import BAND, DAY, T0, point, snapshot, steps
from sqlalchemy import select
from starlette.responses import JSONResponse
from support.clock import freeze_app_clock

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.google_health.client import Page
from app.google_health.data_types import REGISTRY
from app.google_health.pagination import PaginationPaused
from app.models.manual import ManualMeasurement, ManualWorkout
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData, SyncState
from app.services.archive import source_deletions, storage
from app.services.archive.manual_flags import record_flag
from app.services.archive.storage import store_records
from app.services.interpretation import interpretation_inputs
from app.services.interpretation.canonical_daily import METHOD
from app.services.providers.ingestion import GoogleSource
from app.services.shared.query_ranges import CivilDateRange
from app.services.umay import codex_health
from app.sync.incremental import IncrementalSync

# Filled from the unchanged implementation, then held fixed through the refactor.
# PostgreSQL returns source times with their offset and SQLite drops it, so the rendered times and
# the fingerprint over them differ; nothing else does. Each backend's output is pinned on its own.
# GitHub-only Apple extension: the digests include the devices field "apple": "off".
EXPECTED = {
    "sqlite": {
        "UTC": "7043c1c74ac20539f6174fbbbfb932065799d60b86b4deb9229edffe1b801f33",
        "Europe/Paris": "468f3aa07cd2061f64189171e88c35900ee7bc68843b6789b5cd7efa36057e0e",
    },
    "postgresql": {
        "UTC": "c285f6cd1b49fca55662ab583885941334ab0d2f3f6446db78ca0eeda9b82ee3",
        "Europe/Paris": "8e96b9a64f28320b17a77558dfdfb3d1179bc4c0ac1473934402ca070345a947",
    },
}


async def seed_inputs(sessions, monkeypatch):
    freeze_app_clock(monkeypatch, T0 + timedelta(days=2))
    raw_keys = {"synthetic/steps/one": 1, "synthetic/steps/gone": 2, "synthetic/weight/one": 3}
    original = storage._raw_row

    def raw_row(*args):
        row = original(*args)
        return row | {"id": UUID(int=raw_keys[row["google_data_point_id"]])}

    monkeypatch.setattr(storage, "_raw_row", raw_row)
    async with sessions.begin() as session:
        first = await store_records(session, REGISTRY["steps"], [steps(), steps()])
        changed = await store_records(
            session, REGISTRY["steps"], [steps(count=150), steps(count=180), steps(count=180)]
        )
        await store_records(session, REGISTRY["steps"], [steps("gone", 77)])
        await store_records(
            session,
            REGISTRY["weight"],
            [
                point(
                    "weight",
                    "one",
                    {"sampleTime": {"physicalTime": T0.isoformat()}, "weightGrams": 72000},
                )
            ],
        )
        session.add_all(
            [
                ManualMeasurement(id=UUID(int=101), metric="weight", value=70, measured_at=T0),
                ManualMeasurement(id=UUID(int=102), metric="weight", value=71, measured_at=T0),
                ManualWorkout(
                    id=UUID(int=103),
                    exercise_type="Yürüyüş",
                    start_time=T0,
                    end_time=T0 + timedelta(hours=1),
                ),
            ]
        )
        await session.flush()
        await record_flag(session, UUID(int=102), "test", True)
    async with sessions.begin() as session:
        deleted = await source_deletions.mark_source_deletions(
            session, REGISTRY["steps"], T0, T0 + timedelta(days=1), T0 + timedelta(days=3)
        )
        revived = await store_records(session, REGISTRY["steps"], [steps(count=180)])
    assert [asdict(s) for s in (first, changed, revived)] == [
        dict(
            inserted=1,
            updated=0,
            unchanged=0,
            rejected=0,
            normalized=1,
            normalization_skipped=0,
            source_deleted=0,
            versions_kept=0,
        ),
        dict(
            inserted=0,
            updated=1,
            unchanged=0,
            rejected=0,
            normalized=1,
            normalization_skipped=0,
            source_deleted=0,
            versions_kept=2,
        ),
        dict(
            inserted=0,
            updated=0,
            unchanged=1,
            rejected=0,
            normalized=0,
            normalization_skipped=0,
            source_deleted=0,
            versions_kept=0,
        ),
    ]
    assert deleted == 2
    async with sessions() as session:
        versions = list(
            await session.scalars(
                select(RawHealthDataVersion.raw_json).order_by(RawHealthDataVersion.version)
            )
        )
        assert versions == [steps(), steps(count=150)]
        raw = list(await session.scalars(select(RawHealthData).order_by(RawHealthData.id)))
        assert [r.raw_json for r in raw] == [
            steps(count=180),
            steps("gone", 77),
            point(
                "weight",
                "one",
                {"sampleTime": {"physicalTime": T0.isoformat()}, "weightGrams": 72000},
            ),
        ]
        assert [r.source_deleted_at is not None for r in raw] == [False, True, False]


@pytest.mark.parametrize("zone", ["UTC", "Europe/Paris"])
async def test_archive_inputs_and_tool_json_stay_identical(sessions, monkeypatch, zone):
    await seed_inputs(sessions, monkeypatch)
    async with sessions() as session:
        backend = session.get_bind().dialect.name
        before = await snapshot(session)
        inputs = await interpretation_inputs.load_inputs(
            session,
            {"steps", "weight"},
            manual=True,
            workouts=True,
            dates=CivilDateRange(DAY, DAY, zone),
        )
        assert [r.id.int for r in inputs.rows] == [1, 3]
        assert [r.id.int for r in inputs.measurements] == [101]
        assert [r.id.int for r in inputs.workouts] == [103]
        assert inputs.source("steps", BAND, "fallback").startswith("fallback")
    result = {"fingerprint": inputs.fingerprint}
    for action in (
        "inventory",
        "devices",
        "records",
        "daily",
        "coverage",
        "intraday",
        "longitudinal",
    ):
        result[action] = await codex_health.read_health(
            sessions,
            dict(
                action=action,
                start_date=str(DAY),
                end_date=str(DAY),
                timezone=zone,
                **({"data_type": "steps", "limit": 1} if action == "records" else {}),
            ),
        )
    async with sessions() as session:
        assert await snapshot(session) == before
    assert result["daily"]["derived"]["method_version"] == METHOD
    # Check the intentional coverage contract change before comparing the legacy
    # fingerprint: all other tool/archive bytes stay pinned on both backends.
    for metric in result["coverage"]["metrics"]:
        if metric["metric_key"] in {"steps", "sleep_seconds", "weight"}:
            assert metric["exclusions"]["invalid_shape"] == 0
            assert "invalid_shape_unknown" not in metric["reason_codes"]
            metric["exclusions"]["invalid_shape"] = None
            metric["reason_codes"].insert(1, "invalid_shape_unknown")
        elif metric["metric_key"] == "muscle":
            assert metric["exclusions"]["invalid_shape"] is None
            metric["reason_codes"].remove("invalid_shape_unknown")
    # Check the method independently; preserve the other bytes for both backend baselines.
    body = JSONResponse(result).body
    assert b"source-daily/6" not in body
    body = body.replace(METHOD.encode(), b"source-daily/6")
    digest = sha256(body).hexdigest()
    assert digest == EXPECTED[backend][zone], (backend, zone, digest)


@pytest.mark.parametrize("ending", ["completed", "paused", "failed"])
async def test_incremental_outcomes_keep_checkpoint_and_resume(sessions, monkeypatch, ending):
    freeze_app_clock(monkeypatch, T0 + timedelta(days=2))
    failure = {"paused": PaginationPaused, "failed": RuntimeError}.get(ending)
    tokens = []

    class Client:
        async def list_data_points(self, spec, start, end, page_token=None):
            tokens.append(page_token)
            if len(tokens) == 1:
                return Page([steps()], "next")
            if failure is not None:
                raise failure("synthetic interruption")
            return Page([], None)

    service = IncrementalSync(
        Settings(), sessions, GoogleSource(Client()), {"steps": REGISTRY["steps"]}
    )
    if failure:
        with pytest.raises(failure):
            await service.run("steps", T0, T0 + timedelta(days=1))
    else:
        stats = await service.run("steps", T0, T0 + timedelta(days=1))
        assert stats.inserted == 1
    async with sessions() as session:
        state = await session.get(SyncState, "steps")
        assert (
            state.status.value
            == {"completed": "completed", "paused": "pending", "failed": "failed"}[ending]
        )
        assert state.records_downloaded == 1
        assert state.last_page_token == (None if ending == "completed" else "next")
        assert state.last_error == ("RuntimeError" if ending == "failed" else None)
    assert service.last_runs["steps"] == dict(
        sequence=1, inserted=1, updated=0, rejected=0, failed=ending != "completed"
    )
    failure = None
    stats = await service.run("steps", T0, T0 + timedelta(days=1))
    assert stats.inserted == stats.updated == 0
    assert tokens == [None, "next", None if ending == "completed" else "next"]
    async with sessions() as session:
        state = await session.get(SyncState, "steps")
        assert state.status.value == "completed" and state.last_error is None
        assert as_utc(state.last_successful_sync) == T0 + timedelta(days=1)
