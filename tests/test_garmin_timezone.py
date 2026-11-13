"""Synthetic UTC+3 archive: missing heart-rate days retain civil day boundaries."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_records import daily_steps, heart_rate, store_page
from sqlalchemy import select
from test_garmin_normalize import MS, archive_day, raw

from app.core.providers import GARMIN, garmin_record_key
from app.core.timeutil import as_utc
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
async def test_adjacent_heart_reversion_updates_inferred_day_boundaries(sessions, offset):
    adjacent = DAY + timedelta(days=offset)
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    await store_page(sessions, "steps", [daily_steps(DAY)], now)
    for index, hours in enumerate((3, 2, 3, 3)):
        midnight = datetime.combine(adjacent, datetime.min.time()) - timedelta(hours=hours)
        body = heart_rate(adjacent)
        body.update(
            startTimestampGMT=midnight.isoformat(),
            endTimestampGMT=(midnight + timedelta(days=1, seconds=-1)).isoformat(),
        )
        await store_page(sessions, "heart-rate", [body], now + timedelta(minutes=index))
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            row = (await session.scalars(select(Steps))).one()
            assert row.source_timezone == f"+0{hours}:00"
            assert as_utc(row.start_time) == datetime(2026, 11, 6, tzinfo=UTC) - timedelta(
                hours=hours
            )
        assert stats.updated == (2 if index in (1, 2) else 0)
    assert stats.unchanged == 2 and stats.inserted == 0
