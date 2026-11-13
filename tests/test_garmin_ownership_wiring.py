"""The production runtime gives Garmin real ownership; pool capacity pays for it."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import pytest
from garmin_ownership_records import (
    assert_partial_normalization,
    at_boundaries,
    normalization_loss,
    raw_loss,
    rows,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.contracts import OwnershipLost
from app.db.engine import create_shared_engine
from app.models.garmin import GarminNormalizationState
from app.runtime import build_runtime
from app.services.providers.garmin_pull import GarminPull
from app.sync.ownership import CheckedSessions


def test_the_runtime_wires_garmin_to_run_ownership(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wiring.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, garmin_enabled=True)
    pull = build_runtime(settings, sessions).garmin
    assert pull is not None
    assert pull._ownership.__qualname__ == "garmin_ownership.<locals>.owned"


def test_garmin_gets_its_two_connections_only_when_enabled():
    url = "postgresql+asyncpg://synthetic:synthetic@localhost/synthetic"
    plain = create_shared_engine(Settings(_env_file=None, database_url=url))
    garmin = create_shared_engine(Settings(_env_file=None, database_url=url, garmin_enabled=True))
    assert garmin.pool.size() == plain.pool.size() + 2


@pytest.fixture
def loss(sessions):
    lost = {}

    @asynccontextmanager
    async def owner(key):
        lost[key] = False

        async def check():
            if lost[key]:
                raise OwnershipLost("synthetic ownership loss")

        yield CheckedSessions(sessions, check)

    async def lose(key):
        lost[key] = True

    return owner, lose


@pytest.mark.parametrize("lane", ["pull", "backfill"])
@pytest.mark.parametrize("kind", ["steps", "activities"])
async def test_http_loss_fences_real_raw_and_observation_writes(
    sessions, tmp_path, monkeypatch, loss, lane, kind
):
    owner, lose = loss
    await raw_loss(sessions, tmp_path, monkeypatch, lane, kind, owner, lose)


@pytest.mark.parametrize("lane", ["pull", "backfill", "normalize"])
@pytest.mark.parametrize("kind", ["steps", "activities"])
@pytest.mark.parametrize("interruption", ["ownership", "cancel", "failure_after_loss"])
async def test_loss_between_normalization_transactions_preserves_completed_views(
    sessions, tmp_path, monkeypatch, loss, lane, kind, interruption
):
    owner, lose = loss

    async def interrupt(key):
        if interruption == "cancel":
            raise asyncio.CancelledError
        await lose(key)
        if interruption == "failure_after_loss":
            raise RuntimeError("synthetic normalization failure after loss")

    runner, boundary, invoke, before, model, result = await normalization_loss(
        sessions, tmp_path, monkeypatch, lane, kind, owner, interrupt
    )
    runner.owned_by(at_boundaries(owner, boundary))
    try:
        failure = {
            "ownership": OwnershipLost,
            "cancel": asyncio.CancelledError,
            "failure_after_loss": RuntimeError,
        }[interruption]
        await assert_partial_normalization(
            sessions, runner, invoke, before, model, result, failure=failure
        )
    finally:
        await runner.aclose()


@pytest.mark.parametrize("failed", [False, True])
async def test_normalization_outcome_cannot_write_state_after_ownership_loss(
    sessions, monkeypatch, loss, failed
):
    owner, lose = loss
    pull = GarminPull(
        Settings(_env_file=None), sessions, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)
    ).owned_by(owner)
    day = date(2026, 1, 1)
    await pull.normalize(day, day)
    before = await rows(sessions, GarminNormalizationState)
    real = pull.source.normalize
    failure = RuntimeError("synthetic normalization failure")

    async def normalize(*args, **kwargs):
        result = await real(*args, **kwargs)
        await lose("normalize")
        if failed:
            raise failure
        return result

    monkeypatch.setattr(pull.source, "normalize", normalize)
    with pytest.raises(RuntimeError if failed else OwnershipLost) as raised:
        await pull.normalize(day, day)
    if failed:
        assert raised.value is failure
    assert await rows(sessions, GarminNormalizationState) == before
