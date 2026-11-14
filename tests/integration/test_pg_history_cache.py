"""An already allocated writer XID must invalidate when it commits, without a new XID."""

import pytest
from derived_records import store
from history_cache_archive import measured_page, seed_minutes, stable_history_snapshot
from test_history_bounds_cache import MIDDLE, checked, payload

from app.google_health.data_types import REGISTRY
from app.services.archive.storage import store_records

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("days", [8, 40])
async def test_pg_history_statement_and_row_counts(pg_sessions, monkeypatch, days):
    await seed_minutes(pg_sessions, days, 4)
    async with stable_history_snapshot(pg_sessions) as readers:
        expected, before = await measured_page(readers, monkeypatch, use_bounds_cache=False)
        first, cold = await measured_page(readers, monkeypatch)
        second, hot = await measured_page(readers, monkeypatch)
    assert expected == first == second
    # GitHub-only Apple extension: one more bounds aggregate (one row) and one more page read.
    assert before == (5 + 2, days * 4 + 1, days * 4)
    assert cold == (7 + 2, days * 4 + 2 + 1, days * 4)
    assert hot == (4 + 1, 1, 0)


async def test_active_writer_commit_changes_visibility(pg_sessions, monkeypatch):
    async with pg_sessions() as writer:
        await store_records(writer, REGISTRY["steps"], [payload("steps", "edge", MIDDLE)])
        # Cache the old, empty visibility while the writer already owns its XID.
        await checked(pg_sessions, monkeypatch, "steps", None, None)
        await writer.commit()
        await checked(pg_sessions, monkeypatch, "steps", MIDDLE, MIDDLE)
    await store(pg_sessions, "steps", payload("steps", "edge", MIDDLE, True))
    await checked(pg_sessions, monkeypatch, "steps", None, None)
