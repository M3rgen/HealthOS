"""Ingestion requires explicit ownership; only tests may opt out of the run lock."""

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.ingestion_stats import NormalizeStats
from app.services.identities.source_chain_locks import BackfillLockUnavailable, backfill_lock
from app.services.providers.garmin_pull import GarminOwnershipRequired, GarminPull

DAY = date(2026, 1, 1)
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def source():
    async def empty(*args, **kwargs):
        return
        yield

    return SimpleNamespace(
        fetch=Mock(side_effect=empty),
        store_raw=AsyncMock(),
        normalize=AsyncMock(return_value=NormalizeStats()),
    )


@pytest.mark.parametrize("lane", ["run", "backfill", "dry-run", "normalize"])
async def test_garmin_without_ownership_fails_before_any_io(lane, source):
    sessions = Mock()
    pull = GarminPull(Settings(_env_file=None), sessions, source=source, clock=lambda: NOW)
    with pytest.raises(
        GarminOwnershipRequired, match="^GarminPull requires explicit run ownership$"
    ):
        if lane == "run":
            await pull.run(["steps"])
        elif lane == "normalize":
            await pull.normalize(DAY, DAY)
        else:
            await pull.backfill(DAY, DAY, kinds=["steps"], dry_run=lane == "dry-run")
    sessions.assert_not_called()
    sessions.begin.assert_not_called()
    for method in vars(source).values():
        method.assert_not_called()


@pytest.mark.parametrize("binding", [{}, {"bind": None}])
async def test_backfill_lock_without_bind_refuses_entry(binding):
    sessions = async_sessionmaker(**binding)
    with pytest.raises(
        BackfillLockUnavailable, match="^backfill_lock requires a bound session factory$"
    ):
        async with backfill_lock(sessions):
            pytest.fail("an unbound factory must not enter the protected body")


@pytest.mark.parametrize("binding", [{}, {"bind": None}])
async def test_backfill_dry_run_still_allows_an_unbound_factory(binding):
    async with backfill_lock(async_sessionmaker(**binding), dry_run=True):
        pass


@pytest.mark.parametrize("lane", ["run", "backfill"])
async def test_explicit_unlocked_factory_runs_ingestion(unlocked_pull, sessions, source, lane):
    pull = unlocked_pull(Settings(_env_file=None), sessions, source=source, clock=lambda: NOW)
    if lane == "run":
        result = await pull.run(["steps"])
        assert not result.failed
    else:
        result = await pull.backfill(DAY, DAY, kinds=["steps"])
        assert not result["failed"]
        assert result["kinds"]["steps"]["complete"]
    source.fetch.assert_called_once()
    source.normalize.assert_awaited_once()
    source.store_raw.assert_not_called()


def test_production_never_references_the_unlocked_test_seam():
    root = Path(__file__).resolve().parents[1]
    references = [
        (path.relative_to(root).as_posix(), line.strip())
        for path in (root / "app").rglob("*.py")
        for line in path.read_text().splitlines()
        if "unlocked_for_tests" in line
    ]
    assert references == [
        (
            "app/db/contracts.py",
            "def unlocked_for_tests(sessions: TransactionSessions) -> RunOwnership:",
        )
    ]
