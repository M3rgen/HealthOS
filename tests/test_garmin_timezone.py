"""Synthetic UTC+3 archive: missing heart-rate days retain civil day boundaries."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_records import FakeGarmin, daily_steps, heart_rate, store_page
from sqlalchemy import event, select
from test_garmin_activity_recovery import pull_at
from test_garmin_normalize import MS, archive_day, raw

from app.core.providers import GARMIN, garmin_record_key
from app.core.timeutil import as_utc
from app.models.garmin import GarminDailyProjection, GarminDayTime
from app.models.health import HeartRate, Steps
from app.services.providers import garmin_readers
from app.services.providers.garmin_normalize import (
    KINDS,
    build_rows,
    day_bounds,
    newest_records,
    normalize_window,
    write_rows,
)
from app.services.providers.garmin_readers import daily_values
from app.services.providers.garmin_time import day_zones, nearest_zone

DAY = date(2026, 11, 6)


@pytest.mark.parametrize(
    "end, expected",
    [
        ("2026-11-06T23:59:59Z", "2026-11-07T00:00:00+00:00"),
        ("2026-11-06T23:59:59.5Z", "2026-11-07T00:00:00.5+00:00"),
        ("2026-11-06T23:59:58Z", "2026-11-06T23:59:58+00:00"),
        ("9999-12-31T23:59:59Z", "2026-11-07T00:00:00+00:00"),
        ("9999-12-31T23:59:59.5Z", "2026-11-07T00:00:00+00:00"),
    ],
)
def test_day_end_conversion_preserves_representable_bounds(end, expected):
    body = {**heart_rate(DAY), "endTimestampGMT": end}
    assert day_bounds(DAY, body)[1] == datetime.fromisoformat(expected)


@pytest.mark.parametrize("own_offset", [False, True])
def test_overflow_fallback_keeps_own_or_nearest_evidenced_offset(own_offset):
    previous = {**heart_rate(DAY - timedelta(days=1)), "startTimestampGMT": "2026-11-04T21:00:00"}
    body = {
        **heart_rate(DAY),
        "endTimestampGMT": "9999-12-31T23:59:59Z",
        "startTimestampGMT": "2026-11-05T22:00:00" if own_offset else "2026-11-05T00:00:00",
    }
    latest = {
        "steps": {str(DAY): daily_steps(DAY)},
        "heart-rate": {str(DAY - timedelta(days=1)): previous, str(DAY): body},
    }
    rows, _ = build_rows(latest, DAY, DAY, "Synthetic")
    row = rows[Steps][0]
    hours = 2 if own_offset else 3
    assert row["source_timezone"] == f"+0{hours}:00"
    assert row["start_time"] == datetime(2026, 11, 6, tzinfo=UTC) - timedelta(hours=hours)
    assert row["end_time"] == row["start_time"] + timedelta(days=1)


async def test_missing_heart_day_uses_nearest_archive_offset_without_rewriting_v1(sessions):
    previous = DAY - timedelta(days=2)
    heart = heart_rate(previous)
    heart["startTimestampGMT"] = "2026-11-03T21:00:00"
    heart["endTimestampGMT"] = "2026-11-04T20:59:59"
    legacy = Steps(
        google_data_point_id=garmin_record_key("steps", DAY.isoformat()),
        source_record_id=f"steps:{DAY}",
        provider=GARMIN,
        device="Synthetic Garmin",
        start_time=datetime(2026, 11, 6, tzinfo=UTC),
        end_time=datetime(2026, 11, 7, tzinfo=UTC),
        count=99,
    )
    async with sessions.begin() as session:
        session.add_all(
            [
                raw("heart-rate", str(previous), heart),
                raw("heart-rate", str(DAY - timedelta(days=1)), {"heartRateValues": []}),
                raw("steps", str(DAY), daily_steps(DAY)),
                legacy,
            ]
        )
    async with sessions() as session:
        before = dict((await session.execute(select(Steps.__table__))).mappings().one())
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        rows = (await session.scalars(select(Steps).order_by(Steps.id))).all()
        current = next(row for row in rows if row.google_data_point_id.endswith(":v2"))
        assert as_utc(current.start_time) == datetime(2026, 11, 5, 21, tzinfo=UTC)
        assert as_utc(current.end_time) == datetime(2026, 11, 6, 21, tzinfo=UTC)
        assert current.source_timezone == "+03:00"
        assert len(rows) == 2 and current.google_data_point_id.endswith(":v2")
        assert (
            dict(
                (await session.execute(select(Steps.__table__).where(Steps.id == legacy.id)))
                .mappings()
                .one()
            )
            == before
        )
        assert await daily_values(session, DAY, DAY, ZoneInfo("Europe/Istanbul")) == {
            DAY: {"steps": 7000.0}
        }
    async with sessions.begin() as session:
        again = await normalize_window(session, DAY, DAY)
    assert again.inserted == again.updated == 0


def test_nearest_evidence_uses_day_offset_and_leaves_unknown_zone_unknown():
    previous = heart_rate(DAY - timedelta(days=1))
    previous.update(startTimestampGMT="2026-11-04T21:00:00", endTimestampGMT="2026-11-05T20:59:59")
    following = heart_rate(DAY + timedelta(days=1))
    following.update(startTimestampGMT="2026-11-06T22:00:00", endTimestampGMT="2026-11-07T21:59:59")
    zones = day_zones({"2026-11-05": previous, "2026-11-07": following, "bad": {}})
    assert nearest_zone(DAY, zones).utcoffset(None) == timedelta(hours=3)
    assert nearest_zone(DAY + timedelta(days=1), zones).utcoffset(None) == timedelta(hours=2)
    assert nearest_zone(DAY, []) is None
    rows, _ = build_rows({"steps": {str(DAY): daily_steps(DAY)}}, DAY, DAY, "Synthetic")
    assert rows[Steps][0]["source_timezone"] is None
    assert rows[Steps][0]["start_time"] == datetime(2026, 11, 6, tzinfo=UTC)


async def test_v1_reader_fallback_and_v2_preference_preserve_every_legacy_row(sessions):
    await archive_day(sessions)
    async with sessions.begin() as session:
        latest = await newest_records(session, KINDS, DAY, DAY)
        rows, _ = build_rows(latest, DAY, DAY, "Synthetic")
        for model, values in rows.items():
            for value in values:
                value["google_data_point_id"] = value["google_data_point_id"].removesuffix(":v2")
                value["source_timezone"] = None
            if model is HeartRate:
                values[0].update(is_quarantined=True, quarantine_reason="Synthetic exclusion")
            await write_rows(session, model, values)
    zone = ZoneInfo("UTC")
    start = datetime.fromtimestamp(MS / 1000, UTC)
    stop = start + timedelta(days=1)
    async with sessions() as session:
        before = {
            model: [
                dict(row) for row in (await session.execute(select(model.__table__))).mappings()
            ]
            for model in rows
        }
        daily = await daily_values(session, DAY, DAY, zone)
        heart = await garmin_readers.heart_rate_samples(session, start, stop)
        workouts = await garmin_readers.workouts(session, start, stop)
    assert daily[DAY]["steps"] == 7000 and len(heart) == 1 and len(workouts) == 1
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    async with sessions() as session:
        for model, original in before.items():
            kept = (
                await session.execute(
                    select(model.__table__).where(model.id.in_([row["id"] for row in original]))
                )
            ).mappings()
            assert {row["id"]: dict(row) for row in kept} == {row["id"]: row for row in original}
        assert await daily_values(session, DAY, DAY, zone) == daily
        assert await garmin_readers.heart_rate_samples(session, start, stop) == heart
        current_workouts = await garmin_readers.workouts(session, start, stop)
        assert len(current_workouts) == 1
        assert {k: v for k, v in current_workouts[0].items() if k != "id"} == {
            k: v for k, v in workouts[0].items() if k != "id"
        }


@pytest.mark.parametrize("offset", [-1, 1])
async def test_adjacent_heart_reversion_updates_inferred_day_boundaries(sessions, tmp_path, offset):
    adjacent = DAY + timedelta(days=offset)
    now = datetime(2026, 11, 6, 12, tzinfo=UTC)
    await store_page(sessions, "steps", [daily_steps(DAY)], now)
    original_inputs = None
    for index, hours in enumerate((3, 2, 3, 3)):
        midnight = datetime.combine(adjacent, datetime.min.time()) - timedelta(hours=hours)
        body = heart_rate(adjacent)
        body.update(
            startTimestampGMT=midnight.isoformat(),
            endTimestampGMT=(midnight + timedelta(days=1, seconds=-1)).isoformat(),
        )
        # Equal observation times still select by observation id on the return to A.
        await store_page(sessions, "heart-rate", [body], now + timedelta(minutes=min(index, 1)))
        runner = pull_at(
            sessions, tmp_path, FakeGarmin(), now + timedelta(days=0 if index == 0 else 30 + index)
        )
        try:
            stats = (await runner.run(["steps"])).normalized
        finally:
            await runner.aclose()
        async with sessions() as session:
            row = (await session.scalars(select(Steps))).one()
            assert as_utc(row.end_time) == as_utc(row.start_time) + timedelta(days=1)
            inputs = await session.scalar(
                select(GarminDailyProjection.raw_record_ids)
                .where(GarminDailyProjection.source_record_id == f"steps:{DAY}")
                .order_by(GarminDailyProjection.id.desc())
                .limit(1)
            )
            original_inputs = original_inputs or inputs
            assert inputs == original_inputs
            choices = (
                await session.scalars(
                    select(GarminDayTime)
                    .where(GarminDayTime.day == str(DAY))
                    .order_by(GarminDayTime.id)
                )
            ).all()
            assert len(choices) == (1, 2, 3, 3)[index]
            if index >= 2:
                assert choices[-1].raw_record_id == choices[0].raw_record_id
            assert row.source_timezone == f"+0{hours}:00"
            assert as_utc(row.start_time) == datetime(2026, 11, 6, tzinfo=UTC) - timedelta(
                hours=hours
            )
        for table in ("steps", "distance"):
            assert stats.tables.get(table, {}).get("updated", 0) == (1 if index in (1, 2) else 0)
    assert stats.unchanged == 0 and stats.inserted == 0


def offset_heart(day, hours):
    midnight = datetime.combine(day, datetime.min.time()) - timedelta(hours=hours)
    return {
        **heart_rate(day),
        "startTimestampGMT": midnight.isoformat(),
        "endTimestampGMT": (midnight + timedelta(days=1, seconds=-1)).isoformat(),
    }


@pytest.mark.parametrize("direction", [-1, 1])
async def test_periodic_time_choices_handle_first_closer_tie_and_invalid_evidence(
    sessions, tmp_path, direction
):
    from garmin_records import daily_summary
    from test_garmin_activity_results import snapshot

    from app.models.garmin import GarminTimeEvidence
    from app.models.health import Distance, ScalarMetric

    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    far, close = (DAY + timedelta(days=direction * distance) for distance in (4, 1))
    opposite = DAY - timedelta(days=direction)
    for kind, body in (
        ("steps", daily_steps(DAY)),
        ("heart-rate", {"calendarDate": str(DAY), "heartRateValues": []}),
        ("daily-summaries", daily_summary(DAY)),
    ):
        await store_page(sessions, kind, [body], now)
    # Every transition uses the real periodic pull; the historical window is never requested.
    changes = [
        (None, None, None),
        (far, offset_heart(far, 3), 3),
        (close, offset_heart(close, 2), 2),
        (opposite, offset_heart(opposite, 4), 2 if direction == -1 else 4),
        (close, {"calendarDate": str(close)}, 4),
        (opposite, {"calendarDate": str(opposite)}, 3),
        (far, {"calendarDate": str(far)}, None),
        (close, offset_heart(close, 2), 2),
    ]
    models = (Steps, Distance, ScalarMetric, GarminDayTime)
    engine = sessions.kw["bind"].sync_engine
    writes = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and (compiled.isinsert or compiled.isupdate):
            if compiled.statement.table.name in {
                *(model.__tablename__ for model in models),
                GarminTimeEvidence.__tablename__,
                "garmin_daily_projections",
                "garmin_heart_sets",
                "garmin_heart_members",
            }:
                writes.append(statement)

    for index, (key, body, hours) in enumerate(changes):
        if key is not None:
            # Equal observed_at exercises ordering by observation id, also on reversion.
            await store_page(sessions, "heart-rate", [body], now)
        fake = FakeGarmin()
        runner = pull_at(sessions, tmp_path, fake, now + timedelta(days=30 + index))
        try:
            await runner.run(["steps"])
            before = await snapshot(sessions, models)
            writes.clear()
            event.listen(engine, "before_cursor_execute", capture)
            try:
                again = (await runner.run(["steps"])).normalized
            finally:
                event.remove(engine, "before_cursor_execute", capture)
            assert writes == []
            assert again.inserted == again.updated == again.unchanged == 0
            assert await snapshot(sessions, models) == before
        finally:
            await runner.aclose()
        assert all(str(DAY) not in str(request.url) for request in fake.requests)
        async with sessions() as session:
            for model in (Steps, Distance, ScalarMetric):
                rows = (await session.scalars(select(model))).all()
                assert rows
                for row in rows:
                    expected = datetime.combine(DAY, datetime.min.time(), UTC) - timedelta(
                        hours=hours or 0
                    )
                    assert as_utc(row.start_time) == expected
                    assert as_utc(row.end_time) == expected + timedelta(days=1)
                    assert row.source_timezone == (f"+0{hours}:00" if hours else None)
            receipts = (
                await session.scalars(
                    select(GarminDayTime)
                    .where(GarminDayTime.day == str(DAY))
                    .order_by(GarminDayTime.id)
                )
            ).all()
            assert len(receipts) == index + 1 - (index >= (3 if direction == -1 else 4))


async def test_malformed_daily_keys_inside_window_remain_archived_without_time_choices(sessions):
    await store_page(sessions, "steps", [daily_steps(DAY)], datetime(2026, 11, 8, tzinfo=UTC))
    async with sessions.begin() as session:
        session.add(raw("steps", f"{DAY}x", {"totalSteps": 1}))
    async with sessions.begin() as session:
        stats = await normalize_window(session, DAY, DAY + timedelta(days=1))
        assert stats.inserted == 2
    async with sessions() as session:
        choices = (await session.scalars(select(GarminDayTime))).all()
        assert len(choices) == 1 and choices[0].day == str(DAY)
