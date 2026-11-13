"""Completed activity results, exercised through direct and paged normalization."""

from datetime import UTC, datetime, timedelta

import pytest
from garmin_records import activity, store_page
from sqlalchemy import select, update
from test_garmin_normalize import DAY, NOW

from app.models.garmin import (
    GarminActivityResult,
    GarminApiResponse,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import ExerciseSession
from app.services.providers import garmin_bounded, garmin_readers
from app.services.providers.garmin_normalize import build_rows, normalize_window
from app.services.providers.garmin_versions import current_row

START = datetime.combine(DAY, datetime.min.time(), UTC)
STOP = START + timedelta(days=1)
EVIDENCE = (GarminApiResponse, GarminRawRecord, GarminRecordObservation, GarminActivityResult)
INVALID = (
    {"startTimeGMT": None},
    {"startTimeGMT": "broken"},
    {"duration": 0},
    {"duration": None},
    {"duration": 0, "endTimeGMT": "broken"},
    {"duration": 0, "endTimeGMT": f"{DAY}T17:00:00Z"},
    {"duration": None, "endTimeGMT": f"{DAY}T16:00:00Z"},
    {"duration": "1e30"},
    {"duration": "1e1000"},
    {"startTimeGMT": "9999-12-31T23:59:50", "duration": 60},
    {"startTimeGMT": "missing"},
    {"duration": "missing"},
)


def invalid_body(changes):
    body = {**activity(1, DAY), **changes}
    for name, value in changes.items():
        if value == "missing":
            body.pop(name)
    return body


async def normalize(sessions, mode, **kwargs):
    if mode == "bounded":
        return await garmin_bounded.normalize_bounded(sessions, DAY, DAY, device="Garmin", **kwargs)
    async with sessions.begin() as session:
        return await normalize_window(session, DAY, DAY, **kwargs)


async def snapshot(sessions, models):
    async with sessions() as session:
        return {
            model: (await session.execute(select(model.__table__).order_by(model.id))).all()
            for model in models
        }


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("changes", INVALID)
async def test_invalid_correction_and_return_use_completed_results(
    sessions, monkeypatch, mode, changes
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 2)
    a, b = activity(1, DAY), invalid_body(changes)
    previous = {model: [] for model in EVIDENCE}
    original_rows = None
    for index, payload in enumerate((a, b, b, a)):
        seen = NOW + timedelta(minutes=index)
        # The last key goes on the next page even when the first key is invalid.
        await store_page(
            sessions, "activities", [payload, activity(2, DAY), activity(3, DAY)], seen
        )
        stats = await normalize(sessions, mode, seen_since=seen)
        async with sessions() as session:
            visible = await garmin_readers.workouts(session, START, STOP)
            assert {row["id"] for row in visible} == {
                str(row.id)
                for row in await session.scalars(
                    select(ExerciseSession).where(current_row(ExerciseSession))
                )
            }
            assert len(visible) == (3 if index in (0, 3) else 2)
            states = (
                await session.scalars(
                    select(GarminActivityResult)
                    .where(GarminActivityResult.source_record_id == "activities:1")
                    .order_by(GarminActivityResult.id)
                )
            ).all()
            assert len(states) == (1, 2, 2, 3)[index]
            assert (states[-1].normalized_key is not None) == (index in (0, 3))
            selected = await session.get(GarminRawRecord, states[-1].raw_record_id)
            assert selected.body == payload
            if index == 3:
                assert states[-1].raw_record_id == states[0].raw_record_id
                assert states[1].raw_record_id != states[0].raw_record_id
        assert stats.skipped == (1 if index in (1, 2) else 0)
        stored = await snapshot(sessions, (ExerciseSession,))
        original_rows = stored if original_rows is None else original_rows
        assert stored == original_rows  # no overwriting or quarantine on invalidation/restoration
        saved = await snapshot(sessions, EVIDENCE)
        for model in EVIDENCE:
            assert saved[model][: len(previous[model])] == previous[model]
        if index == 2:
            assert saved[GarminActivityResult] == previous[GarminActivityResult]
            assert stats.inserted == stats.updated == 0
        previous = saved
    assert [len(previous[model]) for model in EVIDENCE] == [4, 4, 12, 5]


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("duration", [None, 0, -1, "broken", "1e30", "1e1000"])
async def test_explicit_end_is_valid_independently_of_optional_duration(sessions, mode, duration):
    a = activity(1, DAY)
    b = {**a, "duration": duration, "endTimeGMT": f"{DAY}T18:00:00Z"}
    for index, payload in enumerate((a, b)):
        await store_page(sessions, "activities", [payload], NOW + timedelta(minutes=index))
        stats = await normalize(sessions, mode)
        assert stats.skipped == 0
    async with sessions() as session:
        (visible,) = await garmin_readers.workouts(session, START, STOP)
        assert visible["end_time"] == START + timedelta(hours=18)
        states = (await session.scalars(select(GarminActivityResult))).all()
        assert len(states) == 2 and all(row.normalized_key for row in states)


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_valid_outside_window_and_unseen_keys_publish_no_results(sessions, mode):
    await store_page(sessions, "activities", [activity(1, DAY)], NOW)
    await normalize(sessions, mode)
    before = await snapshot(sessions, (ExerciseSession, GarminActivityResult))
    # A changed but valid activity outside the requested window is not invalid.
    later = NOW + timedelta(minutes=1)
    await store_page(sessions, "activities", [activity(1, DAY - timedelta(days=5))], later)
    await normalize(sessions, mode)
    assert await snapshot(sessions, (ExerciseSession, GarminActivityResult)) == before
    stats = await normalize(sessions, mode, seen_since=later + timedelta(seconds=1))
    if mode == "direct":
        assert await snapshot(sessions, (ExerciseSession, GarminActivityResult)) == before
    else:
        # Periodic recovery also finishes a pending correction outside today's window.
        assert stats.updated == 1
    async with sessions() as session:
        assert len(await garmin_readers.workouts(session, START, STOP)) == (mode == "direct")


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_invalidating_and_restoring_does_not_reopen_user_quarantine(sessions, mode):
    a = activity(1, DAY)
    await store_page(sessions, "activities", [a], NOW)
    await normalize(sessions, mode)
    async with sessions.begin() as session:
        await session.execute(
            update(ExerciseSession).values(
                is_quarantined=True, quarantine_reason="Synthetic exclusion"
            )
        )
    before = await snapshot(sessions, (ExerciseSession,))
    for index, payload in enumerate((invalid_body({"duration": 0}), a), start=1):
        await store_page(sessions, "activities", [payload], NOW + timedelta(minutes=index))
        await normalize(sessions, mode)
        assert await snapshot(sessions, (ExerciseSession,)) == before
        async with sessions() as session:
            assert await garmin_readers.workouts(session, START, STOP) == []


@pytest.mark.parametrize("version", ["v1", "v2", "both"])
async def test_pre_evidence_archives_keep_version_fallback_until_normalized(sessions, version):
    a = activity(1, DAY)
    rows, _ = build_rows({"activities": {"1": a}}, DAY, DAY, "Synthetic")
    v2 = rows[ExerciseSession][0]
    async with sessions.begin() as session:
        if version in ("v1", "both"):
            session.add(
                ExerciseSession(**{**v2, "google_data_point_id": v2["google_data_point_id"][:-3]})
            )
        if version in ("v2", "both"):
            session.add(ExerciseSession(**v2))
    await store_page(sessions, "activities", [invalid_body({"duration": 0})], NOW)
    async with sessions() as session:
        assert len(await garmin_readers.workouts(session, START, STOP)) == 1
    before = await snapshot(sessions, (ExerciseSession,))
    await normalize(sessions, "direct")
    async with sessions() as session:
        assert await garmin_readers.workouts(session, START, STOP) == []
    assert await snapshot(sessions, (ExerciseSession,)) == before


async def test_activity_results_do_not_filter_other_providers_with_the_same_source_key(sessions):
    a = activity(1, DAY)
    rows, _ = build_rows({"activities": {"1": a}}, DAY, DAY, "Synthetic")
    async with sessions.begin() as session:
        for provider in ("GOOGLE", "DEMO"):
            row = rows[ExerciseSession][0]
            session.add(
                ExerciseSession(
                    **{
                        **row,
                        "provider": provider,
                        "google_data_point_id": provider + row["google_data_point_id"],
                    }
                )
            )
    for index, payload in enumerate((a, {**a, "duration": 0})):
        await store_page(sessions, "activities", [payload], NOW + timedelta(minutes=index))
        await normalize(sessions, "direct")
        async with sessions() as session:
            visible = await session.scalars(
                select(ExerciseSession).where(current_row(ExerciseSession))
            )
            assert sorted(row.provider for row in visible) == (
                ["DEMO", "GARMIN", "GOOGLE"] if index == 0 else ["DEMO", "GOOGLE"]
            )
            assert len(await garmin_readers.workouts(session, START, STOP, provider="DEMO")) == 1


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_an_activity_missing_from_a_later_page_remains_current(sessions, mode):
    await store_page(sessions, "activities", [activity(1, DAY)], NOW)
    await normalize(sessions, mode)
    before = await snapshot(sessions, (ExerciseSession, GarminActivityResult))
    later = NOW + timedelta(minutes=1)
    await store_page(sessions, "activities", [activity(2, DAY)], later)
    await normalize(sessions, mode, seen_since=later)
    after = await snapshot(sessions, (ExerciseSession, GarminActivityResult))
    for model in before:
        assert all(row in after[model] for row in before[model])
    async with sessions() as session:
        assert len(await garmin_readers.workouts(session, START, STOP)) == 2
