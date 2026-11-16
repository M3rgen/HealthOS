"""The original-file pull against a generated Garmin HTTP server, without real health data."""

import hashlib
import logging
from datetime import timedelta

import httpx
import pytest
from fit_factory import fit_file
from garmin_records import activity, store_page
from sqlalchemy import func, select
from support.garmin_files import LIST, NOW, PREFIX, pull_service, seed_activities, zipped

from app.db.contracts import OwnershipLost
from app.models.garmin import GarminApiResponse
from app.models.garmin_files import GarminActivityFile, GarminActivityFileResult
from app.services.providers.garmin_file_archive import file_counts


async def counts(sessions):
    async with sessions() as session:
        return await file_counts(session)


async def test_list_pull_downloads_once_and_changed_content_adds_only_new_versions(
    sessions, tmp_path
):
    pull, server, clock = pull_service(sessions, tmp_path)
    body = activity(1, NOW.date())
    server.bodies[LIST] = [body]
    try:
        first = await pull.run(["activities"])
        assert first.activity_files.downloaded == 1 and not first.failed
        assert len(server.downloads()) == 1
        assert await counts(sessions) == {
            "downloaded": 1,
            "pending": 0,
            "unavailable": 0,
            "invalid": 0,
        }
        again = await pull.run(["activities"])
        assert again.activity_files.requests == 0
        assert len(server.downloads()) == 1
        # A changed raw listing revalidates the file, but identical bytes add no file version.
        for revision in (1, 2):
            clock.now += timedelta(hours=2)
            server.bodies[LIST] = [{**body, "revision": revision}]
            if revision == 2:
                server.bodies[PREFIX + "1"] = zipped(fit_file(b"synthetic-message-bytes"))
            report = await pull.run(["activities"])
            assert report.activity_files.downloaded == 1
        assert len(server.downloads()) == 3
        async with sessions() as session:
            rows = (
                await session.scalars(select(GarminActivityFile).order_by(GarminActivityFile.id))
            ).all()
            assert len(rows) == 2
            assert rows[0].body == fit_file(b"")
            assert rows[1].body == fit_file(b"synthetic-message-bytes")
            assert all(
                r.sha256 == hashlib.sha256(r.body).hexdigest() and r.size == len(r.body)
                for r in rows
            )
            assert (
                await session.scalar(select(func.count()).select_from(GarminActivityFileResult))
                == 3
            )
    finally:
        await pull.aclose()


async def test_limit_waits_and_dry_run_are_counted_without_writes(sessions, tmp_path):
    await seed_activities(sessions, 4)
    pull, server, clock = pull_service(
        sessions, tmp_path, garmin_activity_files_per_run=2, garmin_activity_file_interval_seconds=7
    )
    try:
        dry = await pull.activity_files(3, dry_run=True)
        assert dry.requests == 3 and server.requests == [] and clock.waits == []
        assert await counts(sessions) == {
            "downloaded": 0,
            "pending": 4,
            "unavailable": 0,
            "invalid": 0,
        }
        first = await pull.activity_files()
        assert first.requests == first.downloaded == 2 and len(server.downloads()) == 2
        assert clock.waits.count(7) == 1
        assert (await counts(sessions))["pending"] == 2
        second = await pull.activity_files()
        assert second.requests == second.downloaded == 2 and len(server.downloads()) == 4
        assert clock.waits.count(7) == 2
        assert (await pull.activity_files()).requests == 0
    finally:
        await pull.aclose()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_failure_stops_then_next_round_resumes(sessions, tmp_path, status):
    await seed_activities(sessions)
    pull, server, clock = pull_service(sessions, tmp_path)
    server.statuses[PREFIX + "2"] = [status] * 5
    server.headers[PREFIX + "2"] = {"retry-after": "9"}
    try:
        first = await pull.activity_files()
        assert first.downloaded == 1 and first.failed == 1 and first.stopped
        assert [r.url.path for r in server.downloads()] == [PREFIX + "1"] + [PREFIX + "2"] * 5
        assert clock.waits.count(9) == 4
        second = await pull.activity_files()
        assert second.downloaded == 2 and not second.failed
        assert [r.url.path for r in server.downloads()][-2:] == [PREFIX + "2", PREFIX + "3"]
        assert await counts(sessions) == {
            "downloaded": 3,
            "pending": 0,
            "unavailable": 0,
            "invalid": 0,
        }
    finally:
        await pull.aclose()


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_file_is_permanent_even_after_listing_changes(sessions, tmp_path, status):
    await seed_activities(sessions, 1)
    pull, server, clock = pull_service(sessions, tmp_path)
    server.statuses[PREFIX + "1"] = [status]
    try:
        assert (await pull.activity_files()).unavailable == 1
        assert (await pull.activity_files()).requests == 0
        clock.now += timedelta(hours=1)
        await store_page(
            sessions, "activities", [{**activity(1, NOW.date()), "revision": 1}], clock()
        )
        assert (await pull.activity_files()).requests == 0
        assert len(server.downloads()) == 1
        assert await counts(sessions) == {
            "downloaded": 0,
            "pending": 0,
            "unavailable": 1,
            "invalid": 0,
        }
    finally:
        await pull.aclose()


async def test_recovered_retry_also_stops_round_and_keeps_http_metadata(sessions, tmp_path, caplog):
    await seed_activities(sessions, 2)
    pull, server, _ = pull_service(sessions, tmp_path)
    server.statuses[PREFIX + "1"] = [429, 200]
    fit = fit_file(b"SYNTHETIC-PRIVATE-FILE-CONTENT")
    server.bodies[PREFIX + "1"] = zipped(fit, "SYNTHETIC-PRIVATE-NAME.fit")
    try:
        report = await pull.activity_files()
        assert report.stopped and report.downloaded == 1
        assert len(server.downloads()) == 2
        assert (await counts(sessions))["pending"] == 1
        assert "SYNTHETIC-PRIVATE" not in repr([r.__dict__ for r in caplog.records])
        async with sessions() as session:
            responses = (
                await session.scalars(
                    select(GarminApiResponse).where(GarminApiResponse.kind == "activity-files")
                )
            ).all()
            assert len(responses) == 2
            assert all(b"SYNTHETIC-PRIVATE" not in r.body for r in responses)
            assert (
                responses[-1].body_sha256 == hashlib.sha256(server.bodies[PREFIX + "1"]).hexdigest()
            )
        assert (await pull.activity_files()).downloaded == 1
    finally:
        await pull.aclose()


@pytest.mark.parametrize("failure", ["http-403", "owner-lost", "database"])
async def test_file_failures_follow_normalization_and_never_fail_the_list_pull(
    sessions, tmp_path, monkeypatch, failure
):
    pull, server, _ = pull_service(sessions, tmp_path)
    server.bodies[LIST] = [activity(1, NOW.date())]
    server.statuses[PREFIX + "1"] = [403]
    order, normalize, files = [], pull.normalize, pull.activity_files

    async def normalized(*args, **kwargs):
        order.append("normalize")
        return await normalize(*args, **kwargs)

    async def downloaded(*args, **kwargs):
        order.append("files")
        if failure != "http-403":
            raise OwnershipLost("synthetic") if failure == "owner-lost" else RuntimeError()
        return await files(*args, **kwargs)

    monkeypatch.setattr(pull, "normalize", normalized)
    monkeypatch.setattr(pull, "activity_files", downloaded)
    try:
        summary = await pull.run(["activities"])
        assert order == ["normalize", "files"]
        assert not summary.failed and summary.normalized.inserted == 1
        assert summary.activity_files.failed == 1 and summary.activity_files.stopped
        assert await counts(sessions) == {
            "downloaded": 0,
            "pending": 1,
            "unavailable": 0,
            "invalid": 0,
        }
    finally:
        await pull.aclose()


async def test_invalid_file_is_settled_for_its_list_version_only(sessions, tmp_path):
    await seed_activities(sessions, 3)
    pull, server, clock = pull_service(sessions, tmp_path, garmin_activity_files_per_run=2)
    server.bodies[PREFIX + "1"] = zipped(b"not-a-fit-file" * 2)
    try:
        first = await pull.activity_files()
        assert (first.requests, first.invalid, first.downloaded, first.stopped) == (2, 1, 1, False)
        # The invalid file is not first in line again: only the file behind it is requested.
        second = await pull.activity_files()
        assert (second.requests, second.downloaded) == (1, 1)
        assert [r.url.path for r in server.downloads()] == [PREFIX + str(i) for i in (1, 2, 3)]
        assert await counts(sessions) == {
            "downloaded": 2,
            "pending": 0,
            "unavailable": 0,
            "invalid": 1,
        }
        assert (await pull.activity_files()).requests == 0
        # A new list version of the activity is checked again.
        clock.now += timedelta(hours=1)
        await store_page(sessions, "activities", [{**activity(1, NOW.date()), "revision": 1}], NOW)
        server.bodies[PREFIX + "1"] = zipped()
        third = await pull.activity_files()
        assert (third.requests, third.downloaded) == (1, 1)
        assert (await counts(sessions))["invalid"] == 0
    finally:
        await pull.aclose()


async def test_transport_errors_are_never_invalid(sessions, tmp_path):
    await seed_activities(sessions, 2)
    pull, server, _ = pull_service(sessions, tmp_path)

    def handler(request):
        if request.url.path == PREFIX + "1":
            raise httpx.ConnectError("synthetic unreachable")
        return server(request)

    await pull.client.http.aclose()
    pull.client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        report = await pull.activity_files()
        assert (report.failed, report.invalid, report.stopped) == (1, 0, True)
        assert await counts(sessions) == {
            "downloaded": 0,
            "pending": 2,
            "unavailable": 0,
            "invalid": 0,
        }
    finally:
        await pull.aclose()


async def test_logs_name_no_activity_identity_or_content(sessions, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="app")
    identifier, name = 918273645546372819, "SYNTHETIC-PRIVATE-ACTIVITY-NAME"
    pull, server, _ = pull_service(sessions, tmp_path)
    server.bodies[LIST] = [{**activity(identifier, NOW.date()), "activityName": name}]
    server.bodies[PREFIX + str(identifier)] = zipped(fit_file(b"SYNTHETIC-PRIVATE-CONTENT"))
    try:
        summary = await pull.run(["activities"])
        assert summary.activity_files.downloaded == 1
        logged = repr([r.__dict__ for r in caplog.records if r.name.startswith("app.")])
        assert "GARMIN_ACTIVITY_FILE_RESULT" in logged
        for private in (str(identifier), name, "SYNTHETIC-PRIVATE-CONTENT", "synthetic.fit"):
            assert private not in logged
    finally:
        await pull.aclose()
