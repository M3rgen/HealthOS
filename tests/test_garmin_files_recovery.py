"""Failed or unfenced file transactions retain only HTTP evidence and remain resumable."""

from contextlib import asynccontextmanager

import httpx
import pytest
from fit_factory import fit_file
from sqlalchemy import func, select
from support.garmin_files import PREFIX, pull_service, seed_activities, zipped

from app.db.contracts import OwnershipLost, unlocked_for_tests
from app.models.garmin import GarminApiResponse
from app.models.garmin_files import GarminActivityFile, GarminActivityFileResult
from app.services.providers import garmin_activity_files


async def assert_pending(sessions):
    async with sessions() as session:
        for model in (GarminActivityFile, GarminActivityFileResult):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(GarminApiResponse)
                .where(GarminApiResponse.kind.in_(("token", "activity-files")))
            )
            == 2
        )


async def test_lost_owner_preserves_http_without_a_file_or_completion(sessions, tmp_path):
    await seed_activities(sessions, 1)
    pull, server, _ = pull_service(sessions, tmp_path)
    lost = False

    def handler(request):
        nonlocal lost
        response = server(request)
        if request.url.path.startswith(PREFIX):
            lost = True
        return response

    class Fenced:
        @asynccontextmanager
        async def begin(self):
            if lost:
                raise OwnershipLost("synthetic")
            async with sessions.begin() as session:
                yield session

    @asynccontextmanager
    async def ownership(key):
        assert key == "activity-files"
        yield Fenced()

    await pull.client.http.aclose()
    pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pull.owned_by(ownership)
    try:
        with pytest.raises(OwnershipLost):
            await pull.activity_files()
        await assert_pending(sessions)
        pull.owned_by(unlocked_for_tests(sessions))
        assert (await pull.activity_files()).downloaded == 1
    finally:
        await pull.aclose()


async def test_failed_commit_rolls_back_file_and_completion_together(
    sessions, tmp_path, monkeypatch
):
    await seed_activities(sessions, 1)
    pull, _, _ = pull_service(sessions, tmp_path)
    original = garmin_activity_files.store_file_result

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic interrupted publication")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(garmin_activity_files, "store_file_result", interrupted)
            with pytest.raises(RuntimeError, match="synthetic interrupted"):
                await pull.activity_files()
        await assert_pending(sessions)
        assert (await pull.activity_files()).downloaded == 1
    finally:
        await pull.aclose()


async def test_credentials_inside_compressed_fit_are_never_archived(sessions, tmp_path, caplog):
    await seed_activities(sessions, 1)
    pull, server, _ = pull_service(sessions, tmp_path)
    key = pull.settings.garmin_api_key_file.read_text()
    server.bodies[PREFIX + "1"] = zipped(fit_file(key.encode()))
    try:
        result = await pull.activity_files()
        assert result.failed == 1 and result.stopped
        await assert_pending(sessions)
        async with sessions() as session:
            assert all(
                key.encode() not in row.body
                for row in await session.scalars(select(GarminApiResponse))
            )
        assert key not in repr([r.__dict__ for r in caplog.records])
    finally:
        await pull.aclose()
