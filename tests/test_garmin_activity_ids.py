"""Full Garmin activity identities through the real archive and both normalizers."""

from datetime import timedelta

import pytest
from garmin_records import activity, store_page
from sqlalchemy import select, update
from test_garmin_activity_results import EVIDENCE, START, STOP, normalize, snapshot
from test_garmin_normalize import DAY, NOW

from app.models.garmin import GarminActivityResult, GarminRawRecord
from app.models.health import ExerciseSession
from app.services.providers import garmin_bounded, garmin_readers
from app.services.providers.garmin_activity_identity import activity_key
from app.services.providers.garmin_kinds import BY_NAME
from app.services.providers.garmin_normalize import activity_row
from app.services.providers.garmin_versions import current_row

PREFIX = "7" * 64
LONG_IDS = (PREFIX + "101", PREFIX + "102")


def body(identifier, **changes):
    return {**activity(1, DAY), "activityId": identifier, **changes}


@pytest.mark.parametrize("identifier", [0, 123, "123", "a" * 63, PREFIX, "ç" * 64])
def test_short_activity_keys_are_unchanged(identifier):
    record = body(identifier)
    key = str(identifier)
    assert BY_NAME["activities"].key(record) == key
    row = activity_row(record, "Synthetic")
    assert row["source_record_id"] == f"activities:{key}"
    assert row["google_data_point_id"] == f"garmin:activities:{key}:v2"


@pytest.mark.parametrize("identifier", [*LONG_IDS, int(PREFIX + "101"), "ç" * 1024])
def test_long_keys_are_stable_and_fit_every_identity_column(identifier):
    record = body(identifier)
    key = BY_NAME["activities"].key(record)
    row = activity_row(record, "Synthetic")
    assert key == activity_key(body(identifier, calories=999))
    assert key != str(identifier)[:64] and key.startswith("sha256:")
    assert row["source_record_id"] == f"activities:{key}"
    assert row["google_data_point_id"] == f"garmin:activities:{key}:v2"
    for model, name, value in (
        (GarminRawRecord, "key", key),
        (ExerciseSession, "source_record_id", row["source_record_id"]),
        (ExerciseSession, "google_data_point_id", row["google_data_point_id"]),
        (GarminActivityResult, "source_record_id", row["source_record_id"]),
        (GarminActivityResult, "normalized_key", row["google_data_point_id"]),
    ):
        assert len(value) <= model.__table__.c[name].type.length


async def assert_links(sessions, expected):
    """A result, selected immutable body and normalized source agree on full identity."""
    async with sessions() as session:
        rows = (
            await session.scalars(select(ExerciseSession).where(current_row(ExerciseSession)))
        ).all()
        visible = await garmin_readers.workouts(session, START, STOP)
        assert {item["id"] for item in visible} == {
            str(row.id) for row in rows if not row.is_quarantined
        }
        assert {row.source_record_id for row in rows} == {
            f"activities:{activity_key(record)}" for record in expected if record["duration"] > 0
        }
        for record in expected:
            key = activity_key(record)
            state = await session.scalar(
                select(GarminActivityResult)
                .where(GarminActivityResult.source_record_id == f"activities:{key}")
                .order_by(GarminActivityResult.id.desc())
                .limit(1)
            )
            raw = await session.get(GarminRawRecord, state.raw_record_id)
            assert raw.body == record and activity_key(raw.body) == key
            assert state.normalized_key == (
                f"garmin:activities:{key}:v2" if record["duration"] > 0 else None
            )


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("identifiers", [LONG_IDS, tuple(map(int, LONG_IDS))])
@pytest.mark.parametrize("tied_time", [False, True])
async def test_shared_prefix_corrections_refetches_invalidations_and_reversions(
    sessions, monkeypatch, mode, identifiers, tied_time
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 1)
    original, other = (body(identifier) for identifier in identifiers)
    # An actual 64-character id equal to the old prefix must remain independent too.
    short = [body(PREFIX), body(5)]
    changed, invalid = {**original, "calories": 333}, {**original, "duration": 0}
    previous = {model: [] for model in EVIDENCE}
    original_raw = None
    for index, record in enumerate((original, changed, changed, invalid, invalid, original)):
        seen = NOW if tied_time else NOW + timedelta(minutes=index)
        records = [record, other, *short]
        # Separate archive pages, changing arrival order; normalization also has size 1.
        for item in records if index % 2 else reversed(records):
            await store_page(sessions, "activities", [item], seen)
        stats = await normalize(sessions, mode, seen_since=seen)
        assert stats.updated == (1 if index in (1, 5) else 0)
        assert stats.inserted == (4 if index == 0 else 0)
        assert stats.skipped == (1 if index in (3, 4) else 0)
        await assert_links(sessions, records)
        async with sessions() as session:
            states = (
                await session.scalars(
                    select(GarminActivityResult).order_by(GarminActivityResult.id)
                )
            ).all()
            state = next(
                row
                for row in reversed(states)
                if row.source_record_id == f"activities:{activity_key(original)}"
            )
            if index == 0:
                original_raw = state.raw_record_id
            if index == 5:
                assert state.raw_record_id == original_raw
            raw_keys = set(await session.scalars(select(GarminRawRecord.key)))
            assert raw_keys == {activity_key(item) for item in records}
        saved = await snapshot(sessions, EVIDENCE)
        for model in EVIDENCE:
            assert saved[model][: len(previous[model])] == previous[model]
        if index in (2, 4):
            assert saved[GarminActivityResult] == previous[GarminActivityResult]
        previous = saved
        completed = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
        again = await normalize(sessions, mode)
        assert again.inserted == again.updated == 0
        assert await snapshot(sessions, (*EVIDENCE, ExerciseSession)) == completed
    assert [len(previous[model]) for model in EVIDENCE] == [24, 6, 24, 7]


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_one_long_activity_quarantine_survives_correction_and_return(sessions, mode):
    records = [body(identifier) for identifier in LONG_IDS]
    await store_page(sessions, "activities", records, NOW)
    await normalize(sessions, mode)
    source = f"activities:{activity_key(records[0])}"
    async with sessions.begin() as session:
        await session.execute(
            update(ExerciseSession)
            .where(ExerciseSession.source_record_id == source)
            .values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
    for index, change in enumerate(({"calories": 444}, {"duration": 0}, {}), start=1):
        await store_page(
            sessions, "activities", [{**records[0], **change}], NOW + timedelta(minutes=index)
        )
        await normalize(sessions, mode)
        async with sessions() as session:
            (excluded,) = (
                await session.scalars(
                    select(ExerciseSession).where(ExerciseSession.source_record_id == source)
                )
            ).all()
            assert excluded.is_quarantined and excluded.quarantine_reason == "Synthetic exclusion"
            (visible,) = await garmin_readers.workouts(session, START, STOP)
            assert visible["calories_kcal"] == records[1]["calories"]
