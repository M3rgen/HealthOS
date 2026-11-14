"""Step bounds use representable daily sums of current production corrections."""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import BAND, DAY, PHONE, T0, snapshot, steps, store
from numeric_records import store_read_correction
from test_google_platform_daily import DST_DAYS
from test_google_step_accumulation import previous_sum

from app.services.dashboard_views.dashboard_history import history_data
from app.services.interpretation import google_daily_readers as reader
from app.services.readers import CalendarRead, DashboardSources, daily_window
from app.services.readers.reading import ArchiveBounds


@pytest.mark.parametrize("alternative", [None, 0, 7, 1e308])
@pytest.mark.parametrize("reverse", [False, True])
async def test_platform_alternative_and_recovery_restore_bounds(sessions, alternative, reverse):
    old = T0 - timedelta(days=65)
    records = [steps("a", 1e308, old), steps("b", 1e308, old)]
    if alternative is not None:
        records.append(steps("phone", alternative, old, source=PHONE))
    await store_read_correction(sessions, "steps", *(reversed(records) if reverse else records))
    zone = ZoneInfo("UTC")

    async def check(expected):
        async with sessions() as session:
            before = await snapshot(session)
            assert await reader.archive_bounds(session, "steps", DAY, zone) == (
                ArchiveBounds(None, None)
                if expected is None
                else ArchiveBounds(old.date(), old.date())
            )
            page = await history_data(
                session, "steps", 3 if expected is not None else 1, "UTC", DAY
            )
            assert page["total_pages"] == (3 if expected is not None else 1)
            values = [row["value"] for row in page["days"] if row["date"] == str(old.date())]
            assert values == ([expected] if expected is not None else [])
            assert await snapshot(session) == before

    await check(round(alternative) if alternative is not None else None)
    # Replacing one correction rescues the overflowing platform without rewriting
    # either original payload or its prior correction in the raw version archive.
    await store(sessions, "steps", steps("b", 0, old))
    await check(round(1e308))


@pytest.mark.parametrize("zone_name,day,hours,_shares", DST_DAYS)
@pytest.mark.parametrize("counts", [[1e308, 1e308], ["5e-324"], [0.25] * 6, [0]])
async def test_daily_numeric_bounds_across_civil_transitions(
    sessions, zone_name, day, hours, _shares, counts
):
    zone = ZoneInfo(zone_name)
    window = daily_window(day, 1, zone)
    records = [steps(str(i), value, window.start, window.stop) for i, value in enumerate(counts)]
    # Another platform's tiny measurement straddles midnight; neither of its
    # underflowed daily shares can establish an additional contribution day.
    records.append(
        steps(
            "midnight",
            "5e-324",
            window.start - timedelta(hours=1),
            window.start + timedelta(hours=1),
            source=PHONE,
        )
    )
    await store_read_correction(sessions, "steps", *records)
    async with sessions() as session:
        before = await snapshot(session)
        reads = {}
        await reader.read_steps(
            session,
            daily_window(day, 2, zone),
            reads,
            calendar=CalendarRead(day, zone, DashboardSources()),
        )
        assert reads["HEALTH_KIT"].days[day - timedelta(days=1)]["steps"].value is None
        terms = [
            term
            for record in records[:-1]
            for _, term in reader.step_window(record, window.start, window.stop).hourly_terms(zone)
        ]
        value = previous_sum(terms)
        expected = round(value) if value is not None else None
        assert reads["FITBIT"].days[day]["steps"].value == expected
        assert await reader.archive_bounds(session, "steps", day, zone) == (
            ArchiveBounds(None, None) if expected is None else ArchiveBounds(day, day)
        )
        assert (window.stop - window.start).total_seconds() == hours * 3600
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "zone_name,origin",
    [
        ("America/Goose_Bay", "2009-11-01T02:30:00+00:00"),
        ("Pacific/Kwajalein", "1969-09-30T12:00:00+00:00"),
        ("Pacific/Apia", "2011-12-30T09:00:00+00:00"),
    ],
)
async def test_repeated_dates_and_long_intervals_share_daily_arithmetic(
    sessions, zone_name, origin
):
    zone, begin = ZoneInfo(zone_name), datetime.fromisoformat(origin)
    stop = begin + timedelta(days=4)
    records = [steps("long", 1e308, begin, stop)]
    records.extend(
        steps(str(i), 1e308, begin + timedelta(minutes=30 * i), source=PHONE if i % 2 else BAND)
        for i in reversed(range(100))
    )
    await store_read_correction(sessions, "steps", *records)
    # Independent bounded-fixture oracle: the daily reader's pre-streaming sum
    # formula on its civil-hour shares, without early daily or per-share rounding.
    terms = defaultdict(list)
    for record in records:
        source = "HEALTH_KIT" if record["dataSource"] == PHONE else "FITBIT"
        allocation = reader.step_window(record, begin, stop)
        for local, term in allocation.hourly_terms(zone):
            terms[source, local.date()].append(term)
    expected = {
        key: round(value) if (value := previous_sum(values)) is not None else None
        for key, values in terms.items()
    }
    valid = [day for (_, day), value in expected.items() if value is not None]
    today = max(day for _, day in expected)
    async with sessions() as session:
        before = await snapshot(session)
        reads = {}
        await reader.read_steps(
            session,
            daily_window(today, 7, zone),
            reads,
            calendar=CalendarRead(today, zone, DashboardSources()),
        )
        assert {
            (source, day): metrics["steps"].value
            for source, read in reads.items()
            for day, metrics in read.days.items()
        } == expected
        assert await reader.archive_bounds(session, "steps", today, zone) == ArchiveBounds(
            min(valid), max(valid)
        )
        assert await snapshot(session) == before


async def test_overflowing_latest_day_does_not_hide_earlier_measurement(sessions):
    await store(sessions, "steps", steps("sound", 0, T0 - timedelta(days=2)))
    await store_read_correction(sessions, "steps", steps("a", 1e308), steps("b", 1e308))
    async with sessions() as session:
        before = await snapshot(session)
        bounds = await reader.archive_bounds(session, "steps", DAY, ZoneInfo("UTC"))
        assert bounds == ArchiveBounds(DAY - timedelta(days=2), DAY - timedelta(days=2))
        assert await snapshot(session) == before
    await store(sessions, "steps", steps("b", 0))
    async with sessions() as session:
        assert await reader.archive_bounds(session, "steps", DAY, ZoneInfo("UTC")) == ArchiveBounds(
            DAY - timedelta(days=2), DAY
        )
