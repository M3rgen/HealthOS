"""Read old truncated-key evidence without rewriting it or guessing prefix links."""

from dataclasses import replace
from datetime import timedelta

import pytest
from garmin_records import store_page
from sqlalchemy import select, update
from test_garmin_activity_ids import LONG_IDS, PREFIX, assert_links, body
from test_garmin_activity_results import EVIDENCE, START, STOP, normalize, snapshot
from test_garmin_normalize import NOW

from app.core.normalization import payload_hash
from app.models.garmin import GarminActivityResult, GarminRawRecord, GarminRecordObservation
from app.models.health import ExerciseSession
from app.services.providers import garmin_bounded, garmin_normalize, garmin_readers
from app.services.providers.garmin_activity_identity import activity_key
from app.services.providers.garmin_kinds import BY_NAME
from app.services.providers.garmin_normalize import activity_row


async def legacy_page(sessions, monkeypatch, records, seen=NOW):
    """Use the real archive writer with the pre-fix endpoint key function."""
    with monkeypatch.context() as patch:
        patch.setitem(
            BY_NAME,
            "activities",
            replace(BY_NAME["activities"], key=lambda record: str(record["activityId"])[:64]),
        )
        return await store_page(sessions, "activities", records, seen)


def legacy_row(record, version="v2", **extra):
    row = activity_row(record, "Garmin")
    source = f"activities:{record['activityId']}"
    return ExerciseSession(
        **{
            **row,
            "source_record_id": source,
            "google_data_point_id": f"garmin:{source}" + (":v2" if version == "v2" else ""),
            **extra,
        }
    )


async def test_canonical_version_wins_when_same_body_also_has_a_legacy_key(sessions, monkeypatch):
    a, b = [body(identifier) for identifier in LONG_IDS]
    await store_page(sessions, "activities", [a], NOW)
    await legacy_page(sessions, monkeypatch, [a, b])
    before = await snapshot(sessions, (GarminRawRecord,))
    assert (await store_page(sessions, "activities", [a, a, b], NOW)).inserted == 0
    assert await snapshot(sessions, (GarminRawRecord,)) == before
    async with sessions() as session:
        sightings = (
            await session.execute(
                select(GarminRawRecord.key, GarminRawRecord.body_sha256)
                .join(GarminRecordObservation)
                .order_by(GarminRecordObservation.id.desc())
                .limit(2)
            )
        ).all()
    assert set(sightings) == {(activity_key(a), payload_hash(a)), (PREFIX, payload_hash(b))}


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("identifiers", [LONG_IDS, tuple(map(int, LONG_IDS))])
async def test_legacy_versions_split_by_full_id_and_refetch_observes_original_raw_rows(
    sessions, monkeypatch, mode, observed, identifiers
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 1)
    a, b = [body(identifier) for identifier in identifiers]
    correction = {**a, "calories": 321}
    if observed:
        await legacy_page(sessions, monkeypatch, [a, b])
        await legacy_page(sessions, monkeypatch, [correction], NOW + timedelta(minutes=1))
    else:
        async with sessions.begin() as session:
            for record in (a, b, correction):
                session.add(
                    GarminRawRecord(
                        kind="activities",
                        key=PREFIX,
                        body=record,
                        body_sha256=payload_hash(record),
                        first_seen_at=NOW,
                    )
                )
    before = await snapshot(sessions, EVIDENCE[:-1])
    await normalize(sessions, mode)
    await assert_links(sessions, [correction, b])
    assert await snapshot(sessions, EVIDENCE[:-1]) == before
    # Same content under a new key policy reuses its old immutable raw id.
    stable = await snapshot(sessions, (ExerciseSession, GarminActivityResult))
    for index, record in enumerate((correction, a, a), start=2):
        seen = NOW + timedelta(minutes=index)
        stored = await store_page(sessions, "activities", [record], seen)
        assert stored.inserted == 0
        stats = await normalize(sessions, mode, seen_since=seen)
        assert stats.updated == (1 if index == 3 else 0)
        assert stats.inserted == 0
        await assert_links(sessions, [record, b])
        saved = await snapshot(sessions, EVIDENCE[:-1])
        assert saved[GarminRawRecord] == before[GarminRawRecord]
        assert (
            saved[GarminRecordObservation][: len(before[GarminRecordObservation])]
            == before[GarminRecordObservation]
        )
        if index == 2:
            assert await snapshot(sessions, (ExerciseSession, GarminActivityResult)) == stable
    # A genuinely new correction uses the canonical key; both key policies still
    # compete in one full-identity history when the old version returns afterward.
    changed = {**a, "distance": 4321}
    for index, record in enumerate((changed, a), start=5):
        seen = NOW + timedelta(minutes=index)
        assert (await store_page(sessions, "activities", [record], seen)).inserted == (index == 5)
        await normalize(sessions, mode, seen_since=seen)
        await assert_links(sessions, [record, b])
    completed = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    await normalize(sessions, mode)
    assert await snapshot(sessions, (*EVIDENCE, ExerciseSession)) == completed


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_old_results_use_verified_full_id_instead_of_ambiguous_source_prefix(
    sessions, monkeypatch, mode
):
    monkeypatch.setattr(garmin_bounded, "ACTIVITIES_PER_TRANSACTION", 1)
    a, b, short = [body(identifier) for identifier in (*LONG_IDS, PREFIX)]
    await legacy_page(sessions, monkeypatch, [a, b, short])
    invalid = {**b, "duration": 0}
    await legacy_page(sessions, monkeypatch, [invalid], NOW + timedelta(minutes=1))
    async with sessions.begin() as session:
        raw = (await session.scalars(select(GarminRawRecord).order_by(GarminRawRecord.id))).all()
        session.add_all([legacy_row(a), legacy_row(b), legacy_row(short)])
        session.add_all(
            [
                GarminActivityResult(
                    source_record_id=f"activities:{PREFIX}",
                    raw_record_id=raw[0].id,
                    normalized_key=f"garmin:activities:{LONG_IDS[0]}:v2",
                ),
                GarminActivityResult(
                    source_record_id=f"activities:{PREFIX}",
                    raw_record_id=raw[-1].id,
                    normalized_key=None,
                ),
            ]
        )
    async with sessions() as session:
        # The invalid B result must affect B alone, including before any new conversion.
        assert len(await garmin_readers.workouts(session, START, STOP)) == 2
    before = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    await normalize(sessions, mode)
    await assert_links(sessions, [a, invalid, short])
    after = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    for model in before:
        assert all(row in after[model] for row in before[model])
    for index, record in enumerate((b, b, invalid), start=2):
        seen = NOW + timedelta(minutes=index)
        assert (await store_page(sessions, "activities", [record], seen)).inserted == 0
        await normalize(sessions, mode, seen_since=seen)
        await assert_links(sessions, [a, record, short])


@pytest.mark.parametrize("mode", ["direct", "bounded"])
@pytest.mark.parametrize("excluded", ["v1", "v2", "prefix"])
async def test_legacy_quarantine_is_inherited_only_for_exact_full_identity(
    sessions, monkeypatch, mode, excluded
):
    a, b, short = [body(identifier) for identifier in (*LONG_IDS, PREFIX)]
    await legacy_page(sessions, monkeypatch, [a, b, short])
    async with sessions.begin() as session:
        for record in (a, b, short):
            for version in ("v1", "v2"):
                quarantine = (record == a and excluded == version) or (
                    record == short and excluded == "prefix"
                )
                session.add(
                    legacy_row(
                        record,
                        version,
                        is_quarantined=quarantine,
                        quarantine_reason="Synthetic exclusion" if quarantine else None,
                    )
                )
    before = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    await normalize(sessions, mode)
    async with sessions() as session:
        visible = await garmin_readers.workouts(session, START, STOP)
        assert len(visible) == 2  # one quarantine; old full-id rows must not duplicate new ones
        canonical_a = await session.scalar(
            select(ExerciseSession).where(
                ExerciseSession.source_record_id == f"activities:{activity_key(a)}"
            )
        )
        assert canonical_a.is_quarantined == (excluded != "prefix")
    after = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    for model in before:
        assert all(row in after[model] for row in before[model])
    # Restoring a canonical row explicitly is a later user decision; the retained
    # legacy exclusion must not silently replace that decision on future normalizations.
    async with sessions.begin() as session:
        await session.execute(
            update(ExerciseSession)
            .where(ExerciseSession.source_record_id == f"activities:{activity_key(a)}")
            .values(is_quarantined=False, quarantine_reason=None)
        )
    stable = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    await normalize(sessions, mode)
    assert await snapshot(sessions, (*EVIDENCE, ExerciseSession)) == stable


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_mismatched_historical_result_does_not_infer_a_prefix_link(
    sessions, monkeypatch, mode
):
    a, b = [body(identifier) for identifier in LONG_IDS]
    await legacy_page(sessions, monkeypatch, [a, b])
    async with sessions.begin() as session:
        raw = await session.scalar(select(GarminRawRecord).order_by(GarminRawRecord.id))
        session.add_all([legacy_row(a), legacy_row(b)])
        session.add(
            GarminActivityResult(
                source_record_id=f"activities:{PREFIX}",
                raw_record_id=raw.id,
                normalized_key=f"garmin:activities:{LONG_IDS[1]}:v2",
            )
        )
    async with sessions() as session:
        assert len(await garmin_readers.workouts(session, START, STOP)) == 2
    await normalize(sessions, mode)
    await assert_links(sessions, [a, b])


@pytest.mark.parametrize("mode", ["direct", "bounded"])
async def test_legacy_alias_conversion_rolls_back_with_normalized_values(
    sessions, monkeypatch, mode
):
    a, b = [body(identifier) for identifier in LONG_IDS]
    await legacy_page(sessions, monkeypatch, [a, b])
    async with sessions.begin() as session:
        session.add_all([legacy_row(a), legacy_row(b)])
    before = await snapshot(sessions, (*EVIDENCE, ExerciseSession))
    module = garmin_normalize if mode == "direct" else garmin_bounded
    original = module.write_activity_results

    async def interrupted(session, record_ids, results):
        await original(session, record_ids, results)
        assert len(await garmin_readers.workouts(session, START, STOP)) == 2
        raise RuntimeError("synthetic rollback with legacy aliases")

    with monkeypatch.context() as patch:
        patch.setattr(module, "write_activity_results", interrupted)
        with pytest.raises(RuntimeError, match="synthetic rollback"):
            await normalize(sessions, mode)
    assert await snapshot(sessions, (*EVIDENCE, ExerciseSession)) == before
    await normalize(sessions, mode)
    await assert_links(sessions, [a, b])
