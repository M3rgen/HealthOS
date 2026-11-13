"""Garmin pull failures and lag reach /health without disclosing stored error text."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from garmin_records import FakeGarmin, activity
from sqlalchemy import event
from test_garmin_activity_recovery import PATH, pull_at
from test_garmin_activity_results import snapshot

from app.api import garmin as garmin_api
from app.api.health import router
from app.core.config import Settings
from app.garmin.client import GarminClient, GarminUnavailableError
from app.models.garmin import GarminActivityResult, GarminNormalizationState, GarminPullState
from app.models.health import ExerciseSession
from app.models.state import SyncState
from app.runtime import build_runtime
from app.services.providers import garmin_bounded
from app.services.providers.garmin_kinds import KIND_NAMES
from app.services.providers.garmin_normalize_types import NormalizeStats
from app.services.synchronization.pipeline_health import PipelineHealthService, PipelineStatus
from app.services.synchronization.scheduler import build_scheduler

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


async def seed(sessions, when):
    async with sessions.begin() as session:
        session.add_all(
            GarminPullState(kind=name, last_success_at=when, last_attempt_at=when)
            for name in KIND_NAMES
        )


@pytest.fixture
def settings():
    return Settings(
        _env_file=None, app_env="test", healthos_sources="garmin", watchdog_enabled=True
    )


@pytest.fixture
async def health_client(settings, sessions, monkeypatch):
    from support.clock import freeze_app_clock

    freeze_app_clock(monkeypatch, NOW)
    runtime = build_runtime(settings, sessions)
    application = FastAPI()
    application.include_router(router)
    application.state.runtime = runtime
    application.state.settings = settings
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(application), base_url="http://test"
        ) as client:
            yield client, runtime
    finally:
        await runtime.shutdown()


async def test_failed_pull_and_recovery_reach_health(
    settings, sessions, health_client, caplog, unlocked_pull
):
    await seed(sessions, NOW - timedelta(minutes=10))

    class Source:
        failing = True
        normalize = AsyncMock(return_value=NormalizeStats())

        async def fetch(self, stream, window, **kwargs):
            if self.failing:
                raise GarminUnavailableError("synthetic-private-error-detail")
            return
            yield  # An empty stream on recovery.

    source = Source()
    pull = unlocked_pull(settings, sessions, source=source, clock=lambda: NOW)
    assert (await pull.run(kinds=["steps"])).failed
    client, runtime = health_client
    for endpoint in ("/health", "/api/status"):
        response = await client.get(endpoint)
        payload = response.json()
        assert response.status_code == 200
        assert payload["status"] == "degraded"
        assert payload["reasons"] == ["garmin_pull_failed"]
        assert payload["garmin"]["last_success_at"] == "2026-11-09T11:50:00Z"
        assert payload["garmin"]["last_error_at"] == "2026-11-09T12:00:00Z"
        assert payload["garmin"]["last_error_code"] == "garmin_pull_failed"
        assert payload["garmin"]["lag_seconds"] >= 600
        assert payload["watchdog"] == "enabled"
        assert "synthetic-private-error-detail" not in response.text

    runtime.reconciliation.run = AsyncMock(side_effect=AssertionError("Google is disabled"))
    with caplog.at_level(logging.INFO, logger="app.sync.watchdog"):
        outcome = await runtime.watchdog.periodic_check()
    assert outcome.action == "not_needed"
    changed = [r for r in caplog.records if r.message == "WATCHDOG_STATE_CHANGED"]
    assert changed[-1].reasons == ["garmin_pull_failed"]
    runtime.reconciliation.run.assert_not_called()

    source.failing = False
    pull.clock = lambda: NOW + timedelta(minutes=1)
    assert not (await pull.run(kinds=["steps"])).failed
    snapshot = await runtime.pipeline_health.inspect(now=NOW + timedelta(minutes=1))
    assert snapshot.status == PipelineStatus.healthy and snapshot.reasons == ()
    # Recovery leaves historical error evidence visible, but it no longer degrades health.
    assert snapshot.as_dict()["garmin"]["last_error_code"] == "garmin_pull_failed"
    async with sessions() as session:
        state = await session.get(GarminPullState, "steps")
        assert state.last_error == "synthetic-private-error-detail"


@pytest.mark.parametrize("interval", [15, 120, 1440])
async def test_lag_boundary_uses_oldest_kind_and_pull_cadence(settings, sessions, interval):
    settings = settings.model_copy(update={"garmin_pull_interval_minutes": interval})
    threshold = max(120, 2 * interval + 120) * 60
    await seed(sessions, NOW - timedelta(seconds=threshold - 1))
    async with sessions.begin() as session:
        state = await session.get(GarminPullState, "steps")
        state.last_success_at = NOW
    service = PipelineHealthService(settings, sessions, {"steps"})
    before = await service.inspect(now=NOW)
    assert before.status == PipelineStatus.healthy
    assert before.as_dict()["garmin"]["lag_seconds"] == threshold - 1
    after = await service.inspect(now=NOW + timedelta(seconds=1))
    assert after.status == PipelineStatus.degraded
    assert after.reasons == ("garmin_pull_stale",)
    assert after.as_dict()["garmin"]["last_success_at"] == "2026-11-09T12:00:00Z"
    assert after.as_dict()["garmin"]["stale_after_seconds"] == threshold


async def test_missing_or_partially_initialized_state_is_degraded(settings, sessions):
    service = PipelineHealthService(settings, sessions, set())
    for initialized in (False, True):
        if initialized:
            async with sessions.begin() as session:
                session.add(GarminPullState(kind="steps", last_success_at=NOW))
        snapshot = await service.inspect(now=NOW)
        assert snapshot.status == PipelineStatus.degraded
        assert snapshot.reasons == ("garmin_pull_uninitialized",)
        assert snapshot.as_dict()["garmin"]["last_error_code"] is None


@pytest.mark.parametrize("selection", ["none", "google", "apple"])
async def test_deselected_garmin_is_never_queried_or_reported(sessions, selection):
    await seed(sessions, NOW - timedelta(days=30))
    async with sessions.begin() as session:
        state = await session.get(GarminPullState, "steps")
        state.last_error_at = NOW
        state.last_error = "synthetic-private-error-detail"
        session.add(SyncState(data_type="steps", status="completed", last_successful_sync=NOW))
    statements = []
    engine = sessions.kw["bind"].sync_engine

    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", trace)
    try:
        snapshot = await PipelineHealthService(
            Settings(_env_file=None, healthos_sources=selection), sessions, {"steps"}
        ).inspect(now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert snapshot.status == PipelineStatus.healthy
    assert "garmin" not in snapshot.as_dict()
    assert not any("garmin" in reason for reason in snapshot.reasons)
    assert not any("garmin" in sql.lower() for sql in statements)


@pytest.mark.parametrize(
    "google_status, expected", [("completed", "degraded"), ("failed", "failed")]
)
async def test_combined_sources_preserve_google_failure_severity(sessions, google_status, expected):
    await seed(sessions, NOW)
    async with sessions.begin() as session:
        session.add(SyncState(data_type="steps", status=google_status, last_successful_sync=NOW))
        state = await session.get(GarminPullState, "sleep")
        state.last_error_at = NOW + timedelta(seconds=1)
    snapshot = await PipelineHealthService(
        Settings(_env_file=None, healthos_sources="google,garmin"), sessions, {"steps"}
    ).inspect(now=NOW + timedelta(seconds=1))
    assert snapshot.status.value == expected
    assert "garmin_pull_failed" in snapshot.reasons


@pytest.mark.parametrize("selection, enabled", [("garmin", True), ("none", False)])
@pytest.mark.parametrize("watchdog_enabled", [True, False])
def test_watchdog_is_scheduled_for_selected_sources_only(selection, enabled, watchdog_enabled):
    settings = Settings(
        _env_file=None, healthos_sources=selection, watchdog_enabled=watchdog_enabled
    )
    watchdog = SimpleNamespace(periodic_check=AsyncMock())
    scheduler = build_scheduler(settings, SimpleNamespace(), watchdog)
    assert bool(scheduler.get_job("pipeline-watchdog")) == (enabled and watchdog_enabled)


@pytest.mark.parametrize("trigger", ["startup", "watchdog"])
async def test_garmin_watchdog_never_recovers_google(sessions, trigger):
    from support.watchdog import _watchdog

    # Even a stale snapshot carrying an old Google timestamp cannot trigger recovery.
    watchdog, reconciliation = _watchdog(sessions, NOW, healthos_sources="garmin")
    outcome = await watchdog.check(trigger=trigger, now=NOW)
    assert outcome.action == "not_needed" and reconciliation.calls == []


async def normalization_responses(settings, sessions, monkeypatch):
    """Read through a fresh production runtime, after the writer has finished."""
    from support.clock import freeze_app_clock

    runtime = build_runtime(settings, sessions)
    application = FastAPI()
    application.include_router(router)
    application.include_router(garmin_api.router)
    application.state.runtime, application.state.settings = runtime, settings
    application.openapi()
    try:
        with monkeypatch.context() as frozen:
            freeze_app_clock(frozen, NOW)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(application), base_url="http://test"
            ) as client:
                responses = [
                    await client.get(path)
                    for path in ("/health", "/api/status", "/api/garmin/status")
                ]
        assert all(response.status_code == 200 for response in responses)
        return [response.json() for response in responses]
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("lane", ["pull", "backfill", "normalize"])
@pytest.mark.parametrize("error_type", [RuntimeError, GarminUnavailableError])
async def test_normalization_failure_survives_restart_until_normalization_succeeds(
    settings, sessions, tmp_path, monkeypatch, caplog, lane, error_type, unlocked_pull
):
    import asyncio

    await seed(sessions, NOW)
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-key")
    pull = unlocked_pull(
        settings,
        sessions,
        client=GarminClient(
            key,
            http=httpx.AsyncClient(transport=FakeGarmin().transport()),
            sleep=AsyncMock(),
        ),
        clock=lambda: NOW,
    )
    day = NOW.date()
    try:
        await pull.normalize(day, day)
        real = pull.source.normalize
        secret = "synthetic-normalization-secret-and-health-value"
        failure = error_type(secret)
        monkeypatch.setattr(pull.source, "normalize", AsyncMock(side_effect=failure))
        with pytest.raises(error_type) as raised:
            if lane == "pull":
                await pull.run()
            elif lane == "backfill":
                await pull.backfill(day, day)
            else:
                await pull.normalize(day, day)
        assert raised.value is failure
        # Pull success at the same clock instant must not resolve a normalization failure.
        monkeypatch.setattr(pull.source, "normalize", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await pull.run()
        health, status, garmin = await normalization_responses(settings, sessions, monkeypatch)
        for payload in (health, status):
            assert payload["status"] == "degraded"
            assert payload["reasons"] == ["garmin_normalization_failed"]
            assert payload["garmin"]["normalization"] == garmin["normalization"]
        assert set(garmin["kinds"]) == set(KIND_NAMES)
        assert garmin["failing_kinds"] == []
        assert garmin["normalization"]["status"] == "degraded"
        assert garmin["normalization"]["last_error_code"] == "garmin_normalization_failed"
        async with sessions() as session:
            state = await session.get(GarminNormalizationState, 1)
            assert state.last_error_code == "garmin_normalization_failed"
            assert state.last_error_at == state.last_success_at == state.last_attempt_at
        assert secret not in repr((health, status, garmin, state.__dict__))
        assert secret not in repr([record.__dict__ for record in caplog.records])
        monkeypatch.setattr(pull.source, "normalize", real)
        await pull.normalize(day, day)
        recovered = await normalization_responses(settings, sessions, monkeypatch)
        assert all(payload["status"] == "healthy" for payload in recovered[:2])
        assert recovered[2]["normalization"]["last_error_code"] is None
        assert recovered[2]["normalization"]["last_error_at"] is not None
    finally:
        await pull.aclose()


async def test_pending_activity_failure_stays_unhealthy_until_actual_recovery(
    settings, sessions, tmp_path, monkeypatch, caplog
):
    await seed(sessions, NOW)
    fake = FakeGarmin()
    fake.bodies[PATH] = [activity(1, NOW.date())]
    original = garmin_bounded.write_activity_results
    secret = "synthetic-secret-and-health-value"

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError(secret)

    with monkeypatch.context() as patch:
        patch.setattr(garmin_bounded, "write_activity_results", interrupted)
        for attempt in range(2):
            # Fresh pull and client: the second API response contains no activities.
            runner = pull_at(sessions, tmp_path, fake, NOW + timedelta(hours=attempt))
            try:
                with pytest.raises(RuntimeError, match=secret):
                    await runner.run(["activities"])
            finally:
                await runner.aclose()
            fake.bodies[PATH] = []
            responses = await normalization_responses(settings, sessions, monkeypatch)
            for payload in responses[:2]:
                assert payload["status"] == "degraded"
                assert payload["reasons"] == ["garmin_normalization_failed"]
            assert responses[2]["normalization"]["last_error_code"] == "garmin_normalization_failed"
            assert secret not in repr(responses)
            assert all(
                not rows
                for rows in (
                    await snapshot(sessions, (ExerciseSession, GarminActivityResult))
                ).values()
            )
    runner = pull_at(sessions, tmp_path, fake, NOW + timedelta(hours=2))
    try:
        report = await runner.run(["activities"])
        assert report.normalized.inserted == 1
        responses = await normalization_responses(settings, sessions, monkeypatch)
        assert all(payload["status"] == "healthy" for payload in responses[:2])
        assert responses[2]["normalization"]["last_error_code"] is None
        assert secret not in repr([record.__dict__ for record in caplog.records])
    finally:
        await runner.aclose()
