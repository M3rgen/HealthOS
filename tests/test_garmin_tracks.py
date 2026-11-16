"""Content-addressed packages, shared import path, resumable method fills and count-only CLI."""

import asyncio
import hashlib
import json
import logging
import uuid
import zlib
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from fit_factory import START, fit_file, track_activity
from garmin_records import activity, store_page
from sqlalchemy import func, select
from support.garmin_files import NOW, seed_activities

from app.cli import garmin_tracks as cli
from app.core.fit import FIT_FILE, FIT_SOURCE
from app.models.garmin import GarminRawRecord
from app.models.garmin_files import GarminActivityFile, GarminActivityFileResult
from app.models.garmin_tracks import GarminTrack, GarminTrackSession
from app.models.health import ExerciseSession
from app.models.state import RawHealthData
from app.services.archive.storage import store_records
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.providers import garmin_tracks as service
from app.services.providers.fit_import import import_fit_bytes
from app.services.providers.fit_records import parse_fit
from app.services.providers.fit_tracks import METHOD_VERSION, derive_track
from app.services.providers.garmin_file_archive import FileOutcome, store_file_result
from app.services.providers.garmin_track_view import workout_track


async def download(sessions, content, *, key=1):
    async with sessions.begin() as session:
        raw_id = await session.scalar(
            select(GarminRawRecord.id)
            .where(GarminRawRecord.key == str(key))
            .order_by(GarminRawRecord.id.desc())
            .limit(1)
        )
        await store_file_result(session, raw_id, str(key), FileOutcome("downloaded", content), NOW)


async def change_listing(sessions, key=1):
    changed = activity(key, NOW.date())
    changed["distance"] += 10
    await store_page(sessions, "activities", [changed], NOW)


async def packages(sessions):
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(GarminTrack).order_by(
                        GarminTrack.method_version, GarminTrack.sample_count
                    )
                )
            ).all()
        )


async def test_new_file_versions_add_packages_and_a_returning_version_reuses_its_own(sessions):
    await seed_activities(sessions, 1)
    first, second = track_activity(5), track_activity(8)
    await download(sessions, first)
    for content in (second, first):  # A -> B -> A
        await change_listing(sessions)
        await download(sessions, content)
    rows = await packages(sessions)
    assert [row.sample_count for row in rows] == [5, 8]
    for row, content in zip(rows, (first, second), strict=True):
        expected = derive_track(content)
        assert row.source_sha256 == hashlib.sha256(content).hexdigest()
        assert row.status == "ready"
        assert (row.samples, row.laps, row.events, row.summary) == (
            expected.samples,
            expected.laps,
            expected.events,
            expected.summary,
        )
    async with sessions() as session:
        bodies = list(
            await session.scalars(select(GarminActivityFile.body).order_by(GarminActivityFile.id))
        )
    assert bodies == [first, second]
    assert await service.rebuild_tracks(sessions) == 0


async def test_manual_upload_of_a_downloaded_file_shares_its_package(sessions):
    await seed_activities(sessions, 1)
    shared, legacy = track_activity(5), track_activity(8)
    await download(sessions, shared)
    async with sessions.begin() as session:
        await import_fit_bytes(session, shared)
        # Uploaded before tracks existed: archived without a package.
        await store_records(session, FIT_FILE, [parse_fit(legacy).file], source=FIT_SOURCE)
    assert [row.sample_count for row in await packages(sessions)] == [5]
    assert await service.rebuild_tracks(sessions) == 1
    assert await service.rebuild_tracks(sessions) == 0
    assert [row.sample_count for row in await packages(sessions)] == [5, 8]
    async with sessions() as session:
        count = select(func.count()).select_from(RawHealthData)
        assert await session.scalar(count.where(RawHealthData.data_type == FIT_FILE.name)) == 2


async def test_new_method_resumes_after_interruption_and_keeps_old_packages(sessions, monkeypatch):
    await seed_activities(sessions, 3)
    for key in range(1, 4):
        await download(sessions, track_activity(key + 2), key=key)
    before = [(row.source_sha256, row.samples) for row in await packages(sessions)]
    monkeypatch.setattr(service, "METHOD_VERSION", METHOD_VERSION + 1)
    original, calls = service.project_track, 0

    async def interrupt(session, data):
        nonlocal calls
        calls += 1
        result = await original(session, data)
        if calls == 2:
            raise asyncio.CancelledError()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(service, "project_track", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await service.rebuild_tracks(sessions)
    assert len(await packages(sessions)) == 4  # the interrupted input rolled back
    assert await service.rebuild_tracks(sessions) == 2
    rows = await packages(sessions)
    assert [(row.source_sha256, row.samples) for row in rows[:3]] == before
    assert [row.samples for row in rows[3:]] == [samples for _, samples in before]
    assert await service.rebuild_tracks(sessions) == 0


async def test_broken_fit_is_archived_with_completed_invalid_track(sessions):
    await seed_activities(sessions, 1)
    await download(sessions, fit_file(b"\x02"))
    (row,) = await packages(sessions)
    assert (row.status, row.sample_count, row.summary) == ("invalid", 0, {})
    assert await service.rebuild_tracks(sessions) == 0


async def test_projection_failure_never_undoes_the_archive_and_is_rebuilt(
    sessions, monkeypatch, caplog
):
    await seed_activities(sessions, 1)
    content = track_activity(5)
    with monkeypatch.context() as patch:
        patch.setattr(service, "derive_track", Mock(side_effect=RuntimeError("route 1.5,2.5")))
        with caplog.at_level(logging.INFO, logger="app"):
            await download(sessions, content)
            async with sessions.begin() as session:
                await import_fit_bytes(session, track_activity(6))
    assert [r.message for r in caplog.records if "TRACK" in r.message] == [
        "GARMIN_TRACK_DEFERRED",
        "GARMIN_TRACK_DEFERRED",
    ]
    # Only the failure type is logged, never the exception text.
    assert not any("route 1.5,2.5" in str(vars(record)) for record in caplog.records)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(GarminActivityFile)) == 1
        assert await session.scalar(select(func.count()).select_from(GarminActivityFileResult)) == 1
    assert await packages(sessions) == []
    assert await service.rebuild_tracks(sessions) == 2


async def test_damaged_legacy_upload_is_skipped_without_blocking_the_fill(sessions, caplog):
    damaged = parse_fit(track_activity(5)).file
    damaged[FIT_FILE.json_key] = {**damaged[FIT_FILE.json_key], "body": "not base64!"}
    async with sessions.begin() as session:
        await store_records(session, FIT_FILE, [damaged], source=FIT_SOURCE)
        await store_records(
            session, FIT_FILE, [parse_fit(track_activity(7)).file], source=FIT_SOURCE
        )
    with caplog.at_level(logging.INFO, logger="app"):
        assert await service.rebuild_tracks(sessions) == 1
        assert await service.rebuild_tracks(sessions) == 0
    assert [r.message for r in caplog.records].count("GARMIN_TRACK_IMPORT_UNREADABLE") == 2
    assert [row.sample_count for row in await packages(sessions)] == [7]


@pytest.mark.parametrize("failed", [False, True])
def test_cli_prints_only_a_number_even_when_exception_contains_private_content(
    monkeypatch, capsys, failed
):
    monkeypatch.setattr(
        cli,
        "rebuild",
        AsyncMock(
            side_effect=RuntimeError("sensitive synthetic payload") if failed else None,
            return_value=7,
        ),
    )
    assert cli.main(["--rebuild"]) == int(failed)
    captured = capsys.readouterr()
    assert captured.out == ("0\n" if failed else "7\n") and not captured.err


async def listed_tracks(sessions):
    """FIT workout source key -> (has_track, heart-rate column of its track)."""
    found = {}
    async with sessions() as session:
        for row in await list_workouts(session, START.date().replace(day=1), ZoneInfo("UTC")):
            source = await session.scalar(
                select(ExerciseSession.source_record_id).where(
                    ExerciseSession.id == uuid.UUID(row["id"])
                )
            )
            track = await workout_track(session, uuid.UUID(row["id"]))
            hearts = None
            if track is not None:
                hearts = json.loads(zlib.decompress(track.stored.samples))["heart_rate"]
            found[source] = (row["has_track"], hearts)
    return found


def hearts_of(content):
    return json.loads(zlib.decompress(derive_track(content).samples))["heart_rate"]


async def test_upload_links_are_rebuilt_and_the_newest_upload_of_a_workout_wins(sessions):
    first, second = (track_activity(30, seed=seed, intervals=[10]) for seed in (1, 2))
    async with sessions.begin() as session:
        # Archived before workout links existed: the file and its workout, nothing derived.
        parsed = parse_fit(first)
        await store_records(session, FIT_FILE, [parsed.file], source=FIT_SOURCE)
        for spec, records in parsed.records.items():
            await store_records(session, spec, records, source=FIT_SOURCE)
    ((key, (has_track, _)),) = (await listed_tracks(sessions)).items()
    assert key.startswith("fit:") and not has_track
    assert await service.rebuild_tracks(sessions) == 1
    assert await listed_tracks(sessions) == {key: (True, hearts_of(first))}
    for content in (second, first, second):
        async with sessions.begin() as session:
            await import_fit_bytes(session, content)  # same workout, newer upload
        assert await listed_tracks(sessions) == {key: (True, hearts_of(content))}
    async with sessions() as session:
        links = select(func.count()).select_from(GarminTrackSession)
        assert await session.scalar(links) == 2
    assert await service.rebuild_tracks(sessions) == 0
