"""Exercise the adapters' real stages with synthetic HTTP and independent raw tables."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from garmin_records import FakeGarmin, daily_steps
from identity_records import HEART, heart
from sqlalchemy import func, select, true

from app.core.config import Settings
from app.core.fetch_window import FetchWindow, PaginationPaused
from app.core.normalization import payload_hash
from app.garmin.client import GarminClient
from app.google_health.client import GoogleHealthClient, Page
from app.models.garmin import GarminApiResponse, GarminRawRecord
from app.models.health import HeartRate, Steps
from app.models.raw_versions import RawApiResponse
from app.models.state import RawHealthData
from app.parsers.common import payload_hash as google_digest
from app.services.archive.normalization import normalize
from app.services.archive.storage import StoreStats
from app.services.providers.garmin_pull import BY_NAME, day_window, record_digest
from app.services.providers.ingestion import GarminSource, GoogleSource, IngestionSource
from app.services.providers.ingestion_contracts import IngestionPage, ingest_pages
from app.services.readers.dashboard_sources import DashboardSources
from app.services.readers.dashboard_window import daily_window

DAY = date(2026, 11, 6)
START = datetime(2026, 11, 6, tzinfo=UTC)


class Tokens:
    async def get(self):
        return "synthetic-access"


def conform[P: IngestionPage](source: IngestionSource[P]) -> None:
    assert isinstance(source, IngestionSource)
    assert all(callable(stage) for stage in (source.fetch, source.store_raw, source.normalize))
    assert callable(source.readers.heart_rate) and callable(source.readers.workouts)


async def test_google_adapter_preserves_atomic_page_storage_and_reader(sessions):
    point = {"name": "users/me/dataPoints/example", **heart(67, at=START)}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"dataPoints": [point]})
        )
    ) as http:
        source = GoogleSource(GoogleHealthClient(Settings(_env_file=None), Tokens(), http))
        conform(source)
        page = await anext(source.fetch(HEART, FetchWindow(START, START + timedelta(hours=1))))
        parsed = normalize(HEART, page.data_points[0])
        assert parsed is not None and parsed.model is HeartRate
        async with sessions.begin() as session:
            stats = await source.store_raw(session, HEART, page)
            assert stats.inserted == stats.normalized == 1
        async with sessions() as session:
            assert (
                await source.readers.heart_rate(session, START, START + timedelta(hours=1), true())
            ).samples == [(START, 67.0, "FITBIT")]
            assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == 1
            assert await session.scalar(select(func.count()).select_from(GarminRawRecord)) == 0
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 1
        # The page transaction owns both the raw response and the normalized measurement.
        async with sessions.begin() as session:
            other = {"name": "users/me/dataPoints/rolled-back", **heart(70, at=START)}
            await source.store_raw(session, HEART, Page([other], None, page.responses))
            await session.rollback()
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(RawApiResponse)) == 1
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 1
            assert await session.scalar(select(func.count()).select_from(HeartRate)) == 1


async def test_garmin_adapter_preserves_raw_table_and_normalization_boundary(sessions, tmp_path):
    key = tmp_path / "synthetic-key"
    key.write_text("synthetic-key")
    kind = BY_NAME["steps"]
    path, params = kind.request(DAY, DAY)
    fake = FakeGarmin()
    record = daily_steps(DAY)
    fake.bodies[path] = [record]

    async def sleep(delay):
        pass

    client = GarminClient(key, http=httpx.AsyncClient(transport=fake.transport()), sleep=sleep)
    try:
        source = GarminSource(Settings(_env_file=None), sessions, client=client)
        conform(source)
        page = await anext(source.fetch(kind, day_window(DAY, DAY)))
        async with sessions.begin() as session:
            stats = await source.store_raw(session, kind, page)
        assert (stats.inserted, stats.rejected) == (1, 0)
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Steps)) == 0
            assert await session.scalar(select(func.count()).select_from(GarminApiResponse)) == 2
            assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 0
        assert (await source.normalize(day_window(DAY, DAY))).inserted == 2
        async with sessions() as session:
            assert (
                await source.readers.daily(
                    session,
                    daily_window(DAY, 1, ZoneInfo("UTC")),
                    DAY,
                    ZoneInfo("UTC"),
                    DashboardSources(),
                )
            ).days[DAY]["steps"].value == daily_steps(DAY)["totalSteps"]
        assert record_digest(record) == google_digest(record) == payload_hash(record)
        from types import SimpleNamespace

        budget = SimpleNamespace(remaining=0)
        attempted = len(fake.requests)
        with pytest.raises(PaginationPaused):
            await anext(source.fetch(kind, day_window(DAY, DAY), budget=budget))
        assert len(fake.requests) == attempted

        fake.bodies[path] = [daily_steps(DAY, steps=7500)]

        async def rollback(page) -> StoreStats:
            async with sessions.begin() as session:
                await source.store_raw(session, kind, page)
                raise RuntimeError("synthetic transaction failure")

        with pytest.raises(RuntimeError, match="synthetic transaction failure"):
            await anext(ingest_pages(source, kind, day_window(DAY, DAY), rollback))
        async with sessions() as session:
            # The changed record rolled back; the newly received bytes remain evidence.
            assert await session.scalar(select(func.count()).select_from(GarminRawRecord)) == 1
            assert await session.scalar(select(func.count()).select_from(GarminApiResponse)) == 3
            newest = await session.scalar(
                select(GarminApiResponse).order_by(GarminApiResponse.id.desc()).limit(1)
            )
            assert b"7500" in newest.body

    finally:
        await client.aclose()


@pytest.mark.parametrize("provider", ["google", "garmin"])
async def test_same_normalize_and_status_calls_work_for_both_sources(sessions, provider):
    from app.models.garmin import GarminPullState
    from app.models.state import SyncState

    window = FetchWindow(START, START + timedelta(days=1))
    if provider == "google":
        source = GoogleSource(None)
        state = SyncState(data_type="steps", last_successful_sync=START, last_error="synthetic")
    else:
        source = GarminSource(Settings(_env_file=None), sessions)
        state = GarminPullState(kind="steps", last_success_at=START, last_error="synthetic")
    async with sessions.begin() as session:
        session.add(state)
    # Neither source has deferred records, and neither status read requires credentials.
    assert (await source.normalize(window)).as_dict() == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "tables": {},
    }
    async with sessions() as session:
        report = await source.status(session)
    assert report["steps"].last_success_at.replace(tzinfo=UTC) == START
    assert report["steps"].last_error == "synthetic"
