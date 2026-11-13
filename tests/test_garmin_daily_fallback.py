"""Preserve the converters' existing alternate-kind priorities during invalidation."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from garmin_records import store_page
from sqlalchemy import select
from test_garmin_normalize import DAY, NOW, garmin_sleep

from app.models.health import ScalarMetric
from app.services.providers.garmin_bounded import normalize_bounded
from app.services.providers.garmin_normalize import normalize_window
from app.services.providers.garmin_readers import daily_values
from app.services.providers.garmin_versions import current_row


async def normalize(sessions, periodic):
    if periodic:
        later = DAY + timedelta(days=30)
        await normalize_bounded(
            sessions, later, later, device="Garmin", seen_since=NOW + timedelta(days=30)
        )
    else:
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)


@pytest.mark.parametrize("periodic", [False, True])
@pytest.mark.parametrize(
    "primary,alternate,metric,first,second",
    [
        (
            "daily-summaries",
            "heart-rate",
            "resting_bpm",
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
    ],
)
async def test_invalid_preferred_number_uses_valid_alternate(
    sessions, primary, alternate, metric, first, second, periodic
):
    for kind, body in ((primary, first), (alternate, second)):
        await store_page(sessions, kind, [{**body, "calendarDate": str(DAY)}], NOW)
    for index, body in enumerate((first, {}, first)):
        await store_page(
            sessions, primary, [{**body, "calendarDate": str(DAY)}], NOW + timedelta(minutes=index)
        )
        await normalize(sessions, periodic)
        async with sessions() as session:
            if metric == "stress":
                value = (
                    await session.scalars(
                        select(ScalarMetric.value).where(current_row(ScalarMetric))
                    )
                ).one()
                assert value == (30 if index == 1 else 20)
            else:
                days = await daily_values(session, DAY, DAY, ZoneInfo("UTC"))
                assert days[DAY][metric] == (60 if index == 1 else 50)


@pytest.mark.parametrize("periodic", [False, True])
@pytest.mark.parametrize(
    "primary,alternate,metric,body",
    [
        ("sleep", "sleep-summaries", "sleep_seconds", garmin_sleep(DAY)),
        ("hrv-summaries", "hrv", "hrv_ms", {"lastNightAvg": 40}),
    ],
)
async def test_present_invalid_primary_still_blocks_alternate_as_before(
    sessions, primary, alternate, metric, body, periodic
):
    fallback = {"hrvSummary": body} if alternate == "hrv" else body
    await store_page(sessions, alternate, [{**fallback, "calendarDate": str(DAY)}], NOW)
    # With no preferred raw kind, the alternate is used. A nonempty invalid preferred
    # record has always blocked this whole-record fallback; validity must not change it.
    for index, preferred in enumerate(
        (None, {"calendarDate": str(DAY)}, {**body, "calendarDate": str(DAY)})
    ):
        if preferred is not None:
            await store_page(sessions, primary, [preferred], NOW + timedelta(minutes=index))
        await normalize(sessions, periodic)
        async with sessions() as session:
            days = await daily_values(session, DAY, DAY, ZoneInfo("UTC"))
            assert (metric in days.get(DAY, {})) == (index != 1)


async def test_body_battery_max_fallback_and_latest_zero(sessions):
    for index, (battery, expected) in enumerate(
        (
            ({"latestValue": 40, "maxValue": 90}, 40),
            ({"latestValue": None, "maxValue": 90}, 90),
            ({"latestValue": 0, "maxValue": 90}, 0),
        )
    ):
        await store_page(
            sessions,
            "daily-summaries",
            [{"calendarDate": str(DAY), "bodyBattery": battery}],
            NOW + timedelta(minutes=index),
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            assert (
                await session.scalars(select(ScalarMetric.value).where(current_row(ScalarMetric)))
            ).one() == expected
