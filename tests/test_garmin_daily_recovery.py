"""Periodic daily recovery selects pending archive days without refetching their bodies."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from garmin_records import FakeGarmin, daily_steps, store_page
from sqlalchemy import event, select, update
from test_garmin_activity_recovery import DAY, NOW, pull_at
from test_garmin_activity_results import snapshot
from test_garmin_bounded import archive, heart_body
from test_garmin_bounded import sessions as sessions
from test_garmin_daily_projections import CASES, corrected
from test_garmin_timezone import offset_heart

from app.db.contracts import OwnershipLost
from app.models.garmin import (
    GarminDailyProjection,
    GarminDayTime,
    GarminHeartSet,
    GarminRawRecord,
    GarminRecordObservation,
    GarminTimeEvidence,
)
from app.models.health import Distance, HeartRate, ScalarMetric, Steps
from app.services.providers import garmin_bounded, garmin_normalize
from app.services.providers.garmin_current import pending_daily_days
from app.services.providers.garmin_kinds import BY_NAME
from app.services.providers.garmin_membership import write_heart_sets
from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows
from app.services.providers.garmin_versions import current_row
from app.sync.ownership import CheckedSessions

EVIDENCE = (GarminRawRecord, GarminRecordObservation, GarminDailyProjection, GarminHeartSet)


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("completed_chunks", [0, 1])
async def test_restart_recovers_raw_days_after_window_moves_without_another_download(
    sessions, tmp_path, monkeypatch, failure, completed_chunks
):
    days = [DAY - timedelta(days=i) for i in range(5, -1, -1)]
    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 2)
    fake = FakeGarmin()
    path = BY_NAME["steps"].request(days[0], DAY)[0]
    fake.bodies[path] = [daily_steps(day) for day in days]
    original = garmin_bounded.normalize_window
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == completed_chunks + 1:
            raise failure("synthetic interruption before daily conversion")
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(garmin_bounded, "normalize_window", interrupted)
        runner = pull_at(sessions, tmp_path, fake)
        # Fetch all synthetic days before normalization starts.
        runner.settings.garmin_lookback_days = 5
        try:
            with pytest.raises(failure):
                await runner.run(["steps"])
        finally:
            await runner.aclose()
    before = await snapshot(sessions, EVIDENCE)
    assert len(before[GarminRawRecord]) == 6
    assert len(before[GarminDailyProjection]) == completed_chunks * 4
    fake.bodies.clear()
    fake.requests.clear()
    runner = pull_at(sessions, tmp_path, fake, NOW + timedelta(days=30))
    converted, groups = [], []

    async def track(session, start, end, **kwargs):
        converted.append(start)
        if len(converted) > 1:
            assert start == end
        groups.append(session)
        return await original(session, start, end, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(garmin_bounded, "normalize_window", track)
        try:
            recovered = await runner.run(["steps"])
            assert recovered.normalized.inserted == (6 - completed_chunks * 2) * 2
        finally:
            await runner.aclose()
    # The normal empty window spans two days; it is still bounded, then exact pending days.
    assert converted[1:] == days[completed_chunks * 2 :]
    assert max(groups.count(group) for group in groups) <= 2
    after = await snapshot(sessions, EVIDENCE)
    for model in (GarminRawRecord, GarminRecordObservation):
        assert after[model] == before[model]
    assert len(after[GarminDailyProjection]) == 12
    assert (
        after[GarminDailyProjection][: len(before[GarminDailyProjection])]
        == before[GarminDailyProjection]
    )
    assert all(str(DAY) not in str(request.url) for request in fake.requests)


@pytest.mark.parametrize("kind,model,body,path,metric", CASES)
@pytest.mark.parametrize("quarantined", [False, True])
async def test_pending_daily_corrections_reversions_and_invalid_results_are_completed(
    sessions, tmp_path, kind, model, body, path, metric, quarantined
):
    a = {**body, "calendarDate": str(DAY)}
    b = corrected(a, path, "null")
    previous = None
    for index, payload in enumerate((a, b, b, a)):
        # Equal observation timestamps exercise the observation-id tie breaker.
        await store_page(sessions, kind, [payload], NOW)
        runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=30 + index))
        try:
            report = await runner.run([kind])
            assert (await runner.status())["normalization"]["status"] == "healthy"
        finally:
            await runner.aclose()
        async with sessions.begin() as session:
            rows = (
                await session.scalars(
                    select(model).where(current_row(model), ~model.is_quarantined)
                )
            ).all()
            assert bool(rows) == (index == 0 or (not quarantined and index == 3))
            if index == 0 and quarantined:
                await session.execute(
                    update(model).values(
                        is_quarantined=True, quarantine_reason="Synthetic exclusion"
                    )
                )
        saved = await snapshot(sessions, (*EVIDENCE, model))
        if index == 0:
            previous = saved
        else:
            assert saved[model] == previous[model]
        if index == 2:
            assert report.normalized.inserted == report.normalized.updated == 0
            assert saved[GarminDailyProjection] == previous[GarminDailyProjection]
        if index == 3:
            original = saved[GarminDailyProjection][0]
            restored = [
                row
                for row in saved[GarminDailyProjection]
                if row.source_record_id == original.source_record_id
            ][-1]
            assert original.raw_record_ids == restored.raw_record_ids
        previous = saved
        async with sessions() as session:
            assert list(await session.scalars(pending_daily_days())) == []
    assert len(saved[GarminRawRecord]) == 2


@pytest.mark.parametrize("failure", [OwnershipLost, asyncio.CancelledError, RuntimeError])
async def test_pending_recovery_rolls_back_values_projections_and_heart_sets_together(
    sessions, tmp_path, monkeypatch, failure
):
    for offset in range(4):
        day = DAY - timedelta(days=offset)
        await store_page(sessions, "steps", [daily_steps(day)], NOW)
        await store_page(sessions, "heart-rate", [heart_body(day, 60)], NOW)
    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 2)
    original = garmin_normalize.write_daily_projections
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)
        if calls == 4:  # Empty normal window, two committed days, then one uncommitted day.
            raise failure("synthetic lost daily recovery")

    with monkeypatch.context() as patch:
        patch.setattr(garmin_normalize, "write_daily_projections", interrupted)
        runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=30))
        try:
            with pytest.raises(failure):
                await runner.run(["steps"])
        finally:
            await runner.aclose()
    saved = await snapshot(sessions, (*EVIDENCE, Steps, HeartRate))
    assert len(saved[Steps]) == len(saved[GarminHeartSet]) == 2
    assert len(saved[HeartRate]) == 6
    # Each day has two step projections plus an invalid resting-heart-rate projection.
    assert len(saved[GarminDailyProjection]) == 6
    runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=31))
    try:
        expected = "degraded" if failure is RuntimeError else "unknown"
        assert (await runner.status())["normalization"]["status"] == expected
        assert (await runner.run(["steps"])).normalized.inserted == 10
        assert (await runner.status())["normalization"]["status"] == "healthy"
    finally:
        await runner.aclose()


@pytest.mark.parametrize("failure", [OwnershipLost, asyncio.CancelledError])
async def test_ownership_fence_stops_the_next_pending_chunk(
    sessions, tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 1)
    for offset in range(3):
        day = DAY - timedelta(days=offset)
        await store_page(sessions, "steps", [daily_steps(day)], NOW)
    begun = 0

    async def boundary():
        nonlocal begun
        begun += 1
        if begun == 5:  # Two normal chunks, an activity scan, one completed pending day.
            raise failure("synthetic lost owner before next daily chunk")

    @asynccontextmanager
    async def owner(kind):
        yield CheckedSessions(sessions, boundary) if kind == "normalize" else sessions

    runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=30)).owned_by(owner)
    try:
        with pytest.raises(failure):
            await runner.run(["steps"])
    finally:
        await runner.aclose()
    saved = await snapshot(sessions, (Steps, GarminDailyProjection))
    assert len(saved[Steps]) == 1 and len(saved[GarminDailyProjection]) == 2


async def test_sparse_pending_days_do_not_reload_completed_or_raw_only_days(sessions, monkeypatch):
    days = [DAY - timedelta(days=365 * i) for i in range(6, -1, -1)]
    for day in days:
        await store_page(sessions, "steps", [daily_steps(day)], NOW)
        async with sessions.begin() as session:
            await normalize_window(session, day, day, activities=False)
    for day in days[::2]:
        await store_page(sessions, "steps", [daily_steps(day, steps=8000)], NOW)
    # Unobserved legacy input is also pending; raw-only kinds create no work.
    old = days[0] - timedelta(days=365)
    await archive(sessions, "spo2", str(old), {"averageSpO2": 97}, "synthetic-digest")
    await store_page(sessions, "body-battery", [{"calendarDate": str(old)}], NOW)
    loaded, converted = [], []
    original = garmin_bounded.normalize_window

    async def track(session, start, end, **kwargs):
        converted.append((start, end))
        return await original(session, start, end, **kwargs)

    def capture(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and compiled.statement.is_select:
            columns = [column.name for column in compiled.statement.selected_columns]
            # Count normalization inputs; adjacent offset streams select only key/body.
            if "body" in columns and "kind" in columns:
                loaded.append(statement)

    engine = sessions.kw["bind"].sync_engine
    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 2)
    monkeypatch.setattr(garmin_bounded, "normalize_window", track)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for run in range(2):
            loaded.clear()
            converted.clear()
            later = DAY + timedelta(days=30)
            stats = await garmin_bounded.normalize_bounded(
                sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
            )
            assert stats.updated == (8 if run == 0 else 0)
            assert converted[1:] == ([(day, day) for day in [old, *days[::2]]] if run == 0 else [])
            assert len(loaded) == (6 if run == 0 else 1)  # Normal empty window plus pending days.
            async with sessions() as session:
                assert list(await session.scalars(pending_daily_days())) == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize(
    "primary,alternate,projection,first,second",
    [
        (
            "daily-summaries",
            "heart-rate",
            "resting-heart-rate",
            {"heartRate": {"restingValue": 50}},
            {"restingHeartRate": 60},
        ),
        (
            "stress",
            "daily-summaries",
            "stress",
            {"values": {"overallStressLevel": 20}},
            {"stress": {"avgLevel": 30}},
        ),
        ("sleep", "sleep-summaries", "sleep", CASES[3][2], CASES[3][2]),
        ("hrv-summaries", "hrv", "hrv", {"lastNightAvg": 40}, {"hrvSummary": {"lastNightAvg": 60}}),
    ],
)
async def test_changed_alternate_input_is_recovered_even_when_primary_is_present(
    sessions, primary, alternate, projection, first, second
):
    for kind, body in ((primary, first), (alternate, second)):
        await store_page(sessions, kind, [{**body, "calendarDate": str(DAY)}], NOW)
    later = DAY + timedelta(days=30)
    for index, body in enumerate((second, {**second, "syntheticChange": 1}, second)):
        await store_page(sessions, alternate, [{**body, "calendarDate": str(DAY)}], NOW)
        await garmin_bounded.normalize_bounded(
            sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
        )
        async with sessions() as session:
            states = (
                await session.scalars(
                    select(GarminDailyProjection)
                    .where(GarminDailyProjection.source_record_id == f"{projection}:{DAY}")
                    .order_by(GarminDailyProjection.id)
                )
            ).all()
            assert len(states) == index + 1
            assert states[-1].normalized_key is not None
            assert set(states[-1].raw_record_ids) == {primary, alternate}
            assert list(await session.scalars(pending_daily_days())) == []
            if index == 2:
                assert states[0].raw_record_ids == states[-1].raw_record_ids


async def test_empty_heart_sets_are_completed_and_legacy_without_raw_stays_readable(
    sessions, monkeypatch
):
    a = heart_body(DAY, 60)
    orphan = DAY - timedelta(days=100)
    async with sessions.begin() as session:
        rows, _ = build_rows(
            {"steps": {str(orphan): daily_steps(orphan)}}, orphan, orphan, "Garmin"
        )
        await write_rows(session, Steps, rows[Steps])
    legacy = await snapshot(sessions, (Steps,))
    later = DAY + timedelta(days=30)
    for index, body in enumerate((a, {**a, "heartRateValues": []}, a)):
        await store_page(sessions, "heart-rate", [body], NOW)
        stats = await garmin_bounded.normalize_bounded(
            sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
        )
        assert stats.inserted == (3 if index == 0 else 0)
        async with sessions() as session:
            values = (await session.scalars(select(HeartRate).where(current_row(HeartRate)))).all()
            assert len(values) == (0 if index == 1 else 3)
            sets = (await session.scalars(select(GarminHeartSet).order_by(GarminHeartSet.id))).all()
            assert len(sets) == index + 1
            if index == 2:
                assert sets[0].raw_record_id == sets[-1].raw_record_id
            assert list(await session.scalars(pending_daily_days())) == []
            assert (await session.scalars(select(Steps).where(current_row(Steps)))).one()
        assert await snapshot(sessions, (Steps,)) == legacy

    async def unexpected_adjacent_read(*args):
        pytest.fail("an empty periodic window must not reload completed heart bodies")

    monkeypatch.setattr(garmin_normalize, "adjacent_hearts", unexpected_adjacent_read)
    stats = await garmin_bounded.normalize_bounded(
        sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=31)
    )
    assert stats.inserted == stats.updated == stats.unchanged == 0


@pytest.mark.parametrize("completed", ["projection", "heart-set"])
async def test_projection_and_heart_membership_are_independent_completion_requirements(
    sessions, completed
):
    a = heart_body(DAY, 60)
    await store_page(sessions, "heart-rate", [a], NOW)
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY, activities=False)
    await store_page(sessions, "heart-rate", [{**a, "heartRateValues": []}], NOW)
    async with sessions.begin() as session:
        identifier = await session.scalar(
            select(GarminRawRecord.id).order_by(GarminRawRecord.id.desc())
        )
        record_ids = {("heart-rate", str(DAY)): identifier}
        # Represent an archive with only one of the two completed conversion receipts.
        if completed == "projection":
            await garmin_normalize.write_daily_projections(session, record_ids, {})
        else:
            await write_heart_sets(session, record_ids, [])
        assert list(await session.scalars(pending_daily_days())) == [str(DAY)]
    later = DAY + timedelta(days=30)
    await garmin_bounded.normalize_bounded(
        sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
    )
    async with sessions() as session:
        assert list(await session.scalars(pending_daily_days())) == []


async def test_undated_and_malformed_keys_cannot_block_pending_calendar_days(sessions, monkeypatch):
    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 1)
    await store_page(sessions, "steps", [{"totalSteps": 1}], NOW)
    await store_page(sessions, "steps", [{"calendarDate": "2020-99-99", "totalSteps": 2}], NOW)
    await store_page(sessions, "steps", [daily_steps(DAY)], NOW)
    before = await snapshot(sessions, (GarminRawRecord, GarminRecordObservation))
    later = DAY + timedelta(days=30)
    stats = await garmin_bounded.normalize_bounded(
        sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
    )
    assert stats.inserted == 2
    assert await snapshot(sessions, (GarminRawRecord, GarminRecordObservation)) == before


@pytest.mark.parametrize("failure", [OwnershipLost, asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize("completed_chunks", [0, 1])
async def test_time_dependency_recovery_is_atomic_bounded_and_preserves_unrelated_days(
    sessions, tmp_path, monkeypatch, failure, completed_chunks
):
    from garmin_records import daily_summary

    days = [DAY - timedelta(days=i) for i in range(4, 0, -1)]
    adjacent, unrelated = DAY, DAY + timedelta(days=10)
    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 2)
    for day in [*days, unrelated]:
        await store_page(sessions, "steps", [daily_steps(day)], NOW)
        await store_page(sessions, "daily-summaries", [daily_summary(day)], NOW)
    for day, hours in ((adjacent, 3), (unrelated, 1)):
        await store_page(sessions, "heart-rate", [offset_heart(day, hours)], NOW)
    fake = FakeGarmin()
    runner = pull_at(sessions, tmp_path, fake, NOW + timedelta(days=30))
    try:
        await runner.run(["steps"])
    finally:
        await runner.aclose()
    async with sessions.begin() as session:
        await session.execute(
            update(Steps)
            .where(Steps.source_record_id == f"steps:{days[0]}")
            .values(is_quarantined=True, quarantine_reason="Synthetic exclusion")
        )
    models = (*EVIDENCE, GarminDayTime, Steps, Distance, ScalarMetric, HeartRate)
    before = await snapshot(sessions, models)
    await store_page(sessions, "heart-rate", [offset_heart(adjacent, 2)], NOW)
    raw = await snapshot(sessions, (GarminRawRecord, GarminRecordObservation))
    original = garmin_normalize.write_daily_projections
    groups = []

    async def interrupted(session, record_ids, rows, **kwargs):
        await original(session, record_ids, rows, **kwargs)
        keys = {key for kind, key in record_ids}
        if keys & {str(day) for day in days}:
            groups.append(session)
        if str(days[completed_chunks * 2]) in keys:
            raise failure("synthetic interruption after dependent time publication")

    runner = pull_at(sessions, tmp_path, fake, NOW + timedelta(days=31))
    with monkeypatch.context() as patch:
        patch.setattr(garmin_normalize, "write_daily_projections", interrupted)
        try:
            with pytest.raises(failure):
                await runner.run(["steps"])
        finally:
            await runner.aclose()
    assert max(groups.count(group) for group in groups) <= 2
    partial = await snapshot(sessions, models)
    for model in (Steps, Distance, ScalarMetric):
        for old in before[model]:
            new = next(row for row in partial[model] if row.id == old.id)
            committed = old.source_record_id.split(":")[-1] in {
                str(day) for day in days[: completed_chunks * 2]
            }
            if committed:
                assert new.start_time == old.start_time + timedelta(hours=1)
                assert new.end_time == old.end_time + timedelta(hours=1)
                assert new.source_timezone == "+02:00"
                assert new.is_quarantined == old.is_quarantined
                assert new.quarantine_reason == old.quarantine_reason
            else:
                assert new == old
    assert len(partial[GarminDayTime]) == len(before[GarminDayTime]) + completed_chunks * 2
    for model in EVIDENCE:
        assert partial[model] == (raw[model] if model in raw else before[model])
    runner = pull_at(sessions, tmp_path, fake, NOW + timedelta(days=32))
    try:
        await runner.run(["steps"])
        saved = await snapshot(sessions, models)
        again = (await runner.run(["steps"])).normalized
        assert again.inserted == again.updated == again.unchanged == 0
        assert await snapshot(sessions, models) == saved
    finally:
        await runner.aclose()
    assert await snapshot(sessions, (GarminRawRecord, GarminRecordObservation)) == raw
    for model in (Steps, Distance, ScalarMetric):
        assert [row for row in saved[model] if row.source_record_id.endswith(str(unrelated))] == [
            row for row in before[model] if row.source_record_id.endswith(str(unrelated))
        ]
    async with sessions() as session:
        assert list(await session.scalars(pending_daily_days())) == []
        choices = (await session.scalars(select(GarminDayTime).order_by(GarminDayTime.id))).all()
        for day in days:
            states = [row for row in choices if row.day == str(day)]
            assert len(states) == 2 and states[0].raw_record_id != states[1].raw_record_id
    assert all(str(days[0]) not in str(request.url) for request in fake.requests)


@pytest.mark.parametrize("loss", ["before", "inside"])
async def test_time_evidence_index_is_bounded_owned_and_resumable(
    sessions, tmp_path, monkeypatch, loss
):
    from app.services.providers import garmin_time

    monkeypatch.setattr(garmin_bounded, "CHUNK_DAYS", 2)
    days = [DAY - timedelta(days=i) for i in range(7)]
    for day in days:
        await store_page(sessions, "heart-rate", [{"calendarDate": str(day)}], NOW)
    await store_page(sessions, "steps", [daily_steps(DAY)], NOW)
    batches = []
    original = garmin_time.cache_time_evidence

    async def capture(session, rows):
        rows = list(rows)
        batches.append(len(rows))
        await original(session, rows)
        if loss == "inside" and len(batches) == 2:
            raise OwnershipLost("synthetic lost owner during time evidence indexing")

    begun = 0

    async def boundary():
        nonlocal begun
        begun += 1
        if loss == "before" and begun == 4:
            raise OwnershipLost("synthetic lost owner before next evidence batch")

    @asynccontextmanager
    async def owner(kind):
        yield CheckedSessions(sessions, boundary) if kind == "normalize" else sessions

    runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=30)).owned_by(owner)
    with monkeypatch.context() as patch:
        patch.setattr(garmin_time, "cache_time_evidence", capture)
        try:
            with pytest.raises(OwnershipLost):
                await runner.run(["steps"])
        finally:
            await runner.aclose()
    assert begun == 4
    assert batches == ([2] if loss == "before" else [2, 2])
    async with sessions() as session:
        assert len((await session.scalars(select(GarminTimeEvidence))).all()) == 2
        assert not (await session.scalars(select(GarminDayTime))).all()
        assert not (await session.scalars(select(Steps))).all()
    runner = pull_at(sessions, tmp_path, FakeGarmin(), NOW + timedelta(days=31))
    try:
        await runner.run(["steps"])
    finally:
        await runner.aclose()
    async with sessions() as session:
        assert len((await session.scalars(select(GarminTimeEvidence))).all()) == 7
        assert list(await session.scalars(pending_daily_days())) == []
