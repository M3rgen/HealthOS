"""Inject sources at production entry points, using only synthetic archive evidence."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from garmin_records import FakeGarmin, daily_steps
from identity_records import HEART, heart
from raw_response_records import SPEC, body, client, steps, stored
from sqlalchemy import func, select, true

from app.core.config import Settings
from app.core.timeutil import as_utc
from app.garmin.client import GarminClient
from app.google_health.capture import CapturedResponse
from app.google_health.client import Page
from app.google_health.pagination import PageBudget
from app.models.garmin import GarminApiResponse, GarminRawRecord
from app.models.health import HeartRate, Steps
from app.models.raw_versions import RawApiResponse
from app.models.retrieval import RetrievalEvent
from app.models.state import BackfillJob, JobStatus, RawHealthData, SyncState
from app.runtime import build_runtime
from app.services.archive.storage import store_page
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.providers.ingestion import GarminSource, GoogleSource, IngestionSource, PageSource
from app.services.providers.ingestion_readers import GARMIN_ARCHIVE, GOOGLE_ARCHIVE
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.dashboard_window import daily_window
from app.sync.backfill import BackfillService
from app.sync.incremental import IncrementalSync

START = datetime(2026, 11, 6, tzinfo=UTC)
END = START + timedelta(days=1)
DAY = START.date()


class FakePageSource:
    """A third source: no Google client and no provider-specific sync branch."""

    name = "synthetic"
    readers = GOOGLE_ARCHIVE

    def __init__(self, fail=False):
        self.fail = fail
        self.normalize = AsyncMock()  # Page storage owns atomic normalization.
        self.calls = []
        self.keep_after = AsyncMock()
        self.warm_up = AsyncMock()
        self.mark_missing = AsyncMock(return_value=0)
        self.status = AsyncMock(return_value={})
        self.response = CapturedResponse(
            START, "sync", None, HEART.name, "GET", "/synthetic", None, None, 1, 200, {}, b"{}"
        )

    async def fetch(self, spec, window, *, on_reset=None, budget=None, on_request=None):
        self.calls.append(("fetch", spec, window.start, window.end, window.page_token, budget))
        for index, token in enumerate(("next", None)):
            budget.remaining -= 1
            point = {"name": f"users/synthetic/dataPoints/{index}", **heart(67, at=START)}
            yield Page([point], token, responses=(self.response,))

    async def store_raw(self, session, spec, page, **kwargs):
        self.calls.append(("store", page.next_page_token, kwargs))
        stats = await store_page(session, spec, page, **kwargs)
        if self.fail:
            raise RuntimeError("synthetic page rollback")
        return stats


@pytest.mark.parametrize("lane", ["sync", "backfill"])
@pytest.mark.parametrize("fail", [False, True])
async def test_injected_page_source_drives_fetch_store_checkpoint_and_rollback(
    sessions, lane, fail
):
    source = FakePageSource(fail)
    assert isinstance(source, IngestionSource) and isinstance(source, PageSource)
    settings = Settings(_env_file=None, backfill_start_date=DAY, backfill_window_days=1)
    specs = {HEART.name: HEART}
    if lane == "sync":
        service = IncrementalSync(settings, sessions, source, specs)
        run = service.run(HEART.name, START, END)
        state_model = SyncState
    else:
        service = BackfillService(settings, sessions, source, specs)
        await service._ensure_jobs(HEART.name, END, START, sessions)
        run = service._run_next_window(HEART.name, PageBudget())
        state_model = BackfillJob
    if fail:
        with pytest.raises(RuntimeError, match="synthetic page rollback"):
            await run
        source.keep_after.assert_awaited_once()
        assert source.keep_after.call_args.args[0].responses == (source.response,)
    else:
        await run
        source.keep_after.assert_not_awaited()
    fetch = source.calls[0]
    assert (fetch[0], fetch[1], as_utc(fetch[2]), as_utc(fetch[3]), fetch[4]) == (
        "fetch",
        HEART,
        START,
        END,
        None,
    )
    assert source.mark_missing.await_count == int(lane == "sync" and not fail)
    assert [call[1] for call in source.calls[1:]] == (["next"] if fail else ["next", None])
    assert all(call[2]["fetch_started"] is not None for call in source.calls[1:])
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == (
            0 if fail else 2
        )
        assert await session.scalar(select(func.count()).select_from(HeartRate)) == (
            0 if fail else 2
        )
        assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == (
            0 if fail else 2
        )
        state = (await session.scalars(select(state_model))).one()
        assert state.status == (JobStatus.failed if fail else JobStatus.completed)
        events = list(
            await session.scalars(select(RetrievalEvent.kind).order_by(RetrievalEvent.id))
        )
        assert events == (["failed"] if fail else ["page", "page", "completed"])
        if not fail:
            assert len((await source.readers.heart_rate(session, START, END, true())).samples) == 2


class ObservedGarminSource:
    """A replacement source that observes real archiving and normalization boundaries."""

    name = "synthetic-garmin"
    readers = GARMIN_ARCHIVE

    def __init__(self, delegate, sessions):
        self.delegate, self.sessions = delegate, sessions
        self.calls = []

    async def fetch(self, *args, **kwargs):
        self.calls.append("fetch")
        async for page in self.delegate.fetch(*args, **kwargs):
            yield page

    async def keep_after(self, *args, **kwargs):
        await self.delegate.keep_after(*args, **kwargs)

    async def status(self, *args, **kwargs):
        return await self.delegate.status(*args, **kwargs)

    async def store_raw(self, *args, **kwargs):
        self.calls.append("store")
        return await self.delegate.store_raw(*args, **kwargs)

    async def normalize(self, *args, **kwargs):
        self.calls.append(("normalize", kwargs))
        async with self.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(GarminRawRecord)) == 1
            assert await session.scalar(select(func.count()).select_from(GarminApiResponse)) == 2
            assert await session.scalar(select(func.count()).select_from(Steps)) == 0
        return await self.delegate.normalize(*args, **kwargs)


@pytest.mark.parametrize("lane", ["pull", "backfill"])
async def test_garmin_production_entry_points_use_injected_source(
    sessions, tmp_path, lane, unlocked_pull
):
    fake = FakeGarmin()
    first = DAY - timedelta(days=1) if lane == "pull" else DAY
    path = f"/usersummary-service/stats/steps/daily/{first}/{DAY}"
    fake.bodies[path] = [daily_steps(DAY)]
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-key")
    client = GarminClient(key, http=httpx.AsyncClient(transport=fake.transport()))
    settings = Settings(_env_file=None, garmin_enabled=True, garmin_lookback_days=1)
    source = ObservedGarminSource(
        GarminSource(settings, sessions, client=client, clock=lambda: START), sessions
    )
    assert isinstance(source, IngestionSource)
    pull = unlocked_pull(settings, sessions, source=source, clock=lambda: START)
    try:
        if lane == "pull":
            result = await pull.run(["steps"])
            assert not result.failed and result.normalized.inserted == 2
        else:
            result = await pull.backfill(DAY, DAY, kinds=["steps"])
            assert not result["failed"] and result["normalized"]["inserted"] == 2
        assert source.calls[:2] == ["fetch", "store"]
        assert len(source.calls) == 3 and source.calls[2][0] == "normalize"
        assert source.calls[2][1].pop("sessions").sessions is sessions
        assert source.calls[2][1] == {"seen_since": START if lane == "pull" else None}
        assert pull.backend is None  # The injected source owns fetching.
        async with sessions() as session:
            assert (
                await source.readers.daily(
                    session,
                    daily_window(DAY, 1, ZoneInfo("UTC")),
                    DAY,
                    ZoneInfo("UTC"),
                    DashboardSources(),
                )
            )[0].days[DAY]["steps"].value == daily_steps(DAY)["totalSteps"]
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0
            assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == 0
            # The production read model consumes the same source catalog.
            data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"))
            assert data["days"][0]["steps"] == daily_steps(DAY)["totalSteps"]
    finally:
        await client.aclose()


async def test_runtime_composes_one_google_source_for_both_page_lanes(sessions):
    runtime = build_runtime(Settings(_env_file=None, app_env="test"), sessions)
    try:
        source = runtime.incremental.source
        assert isinstance(source, PageSource)
        assert runtime.backfill.source is source
        assert source.client is runtime.google
        assert source.readers is GOOGLE_ARCHIVE
    finally:
        await runtime.shutdown()


async def test_sync_tags_responses_and_keeps_them_when_storing_fails(sessions, monkeypatch) -> None:
    pages = [body(steps(1)), body(steps(2))]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pages.pop(0))

    google = client(handler, sessions)
    incremental = IncrementalSync(
        Settings(_env_file=None, app_env="test"), sessions, GoogleSource(google), {"steps": SPEC}
    )
    await incremental.run("steps")
    (first,) = await stored(sessions)
    assert first.source == "sync" and first.context_id.startswith("sync:")

    import app.services.archive.storage as storage

    async def broken(*args, **kwargs):
        raise RuntimeError("normalizer bug")

    monkeypatch.setattr(storage, "_upsert_normalized_many", broken)
    with pytest.raises(RuntimeError, match="normalizer bug"):
        await incremental.run("steps")
    rows = await stored(sessions)
    assert len(rows) == 2 and rows[1].body == body(steps(2))  # kept although records rolled back
    async with sessions() as session:
        raw = await session.scalar(select(RawHealthData))
    assert raw is not None and raw.raw_json == steps(1) and raw.response_id == first.id
