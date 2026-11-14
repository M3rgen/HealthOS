"""Dense production-ingested step streams retain bucket state, not contribution lists."""

import gc
import tracemalloc
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import product
from zoneinfo import ZoneInfo

import pytest
from derived_records import BAND, DAY, PHONE, T0, steps, store
from numeric_records import store_read_correction
from support.numeric_outputs import api as api
from support.numeric_outputs import outputs
from test_google_platform_daily import DST_DAYS, candidates, project

from app.services.interpretation import google_daily_readers
from app.services.readers import CalendarRead, DashboardSources, daily_window
from app.services.readers.reading import ArchiveBounds
from app.services.shared.finite_math import FiniteScaledSum, finite_calculation


def previous_sum(terms):
    """The pre-streaming formula, including its ordinary float arithmetic."""
    return finite_calculation(
        sum(value * multiplier / divisor for value, multiplier, divisor in terms),
        lambda: sum((Fraction(v) * Fraction(m) / Fraction(d) for v, m, d in terms), Fraction()),
    )


def test_streaming_sum_matches_builtin_compensation_and_recovery():
    # Small addends, both compensation branches, cancellation, overflow and subnormals.
    values = (0.0, 1.0, -1.0, 1e-16, 1e16, -1e16, 1e308, -1e308, 5e-324)
    for counts in product(values, repeat=4):
        total = FiniteScaledSum()
        terms = [(value, 1.0, 1.0) for value in counts]
        for term in terms:
            total.add(*term)
        assert total.result() == previous_sum(terms), counts
        assert total.result() == total.result()  # finalizing does not consume state
        total.add(1.0, 1.0, 1.0)
        assert total.result() == previous_sum([*terms, (1.0, 1.0, 1.0)])


@pytest.mark.parametrize(
    "terms,expected",
    [
        ([(0.1, 3.0, 1.0)], 0.30000000000000004),
        ([(1e308, 3600.0, 3600.0)] * 2 + [(-1e308, 3600.0, 3600.0)], 1e308),
        ([(1e308, 3600.0, 3600.0)] * 2, None),
        ([(5e-324, 3600.0, 7200.0)], None),
        ([(5e-324, 3600.0, 7200.0)] * 2, 5e-324),
        ([(0.0, 3600.0, 7200.0)], 0.0),
        ([(1.0, 1.0, 3.0), (-1.0, 1.0, 7.0), (-1.0, 4.0, 21.0)], 0.0),
    ],
)
def test_scaled_sum_keeps_float_rounding_and_exact_operands(terms, expected):
    total = FiniteScaledSum()
    for term in terms:
        total.add(*term)
    assert total.result() == previous_sum(terms) == expected


@pytest.mark.parametrize(
    "counts,hours,expected",
    [
        ([0.1] * 128, 1, 13),
        ([0.25] * 6, 1, 2),  # round the daily total, never the contributions
        ([0] * 128, 1, 0),
        ([120, -1, "-1e-1000"] * 32, 1, 3840),
        ([1e308, 1e308, -1e308, -1e308] * 32 + [1e308], 1, None),
        (["5e-324"] * 128, 2, 0),
        (["5e-324"], 2, 0),
    ],
)
async def test_dense_numeric_streams_agree_across_all_outputs(
    api, sessions, counts, hours, expected
):
    records = [
        steps(str(i), count=count, end=T0 + timedelta(hours=hours))
        for i, count in enumerate(counts)
    ]
    # Large counts arrive as real corrections; ingestion preserves their valid old
    # normalized rows, raw versions and current payloads. No direct database fixtures.
    await store_read_correction(sessions, "steps", *records)
    daily = await outputs(api, sessions, "steps", [expected], "steps", "steps")
    async with sessions() as session:
        assert await google_daily_readers.archive_bounds(
            session, "steps", DAY, ZoneInfo("UTC")
        ) == (ArchiveBounds(None, None) if expected is None else ArchiveBounds(DAY, DAY))
    hourly = previous_sum(
        [(float(value), 3600.0, hours * 3600.0) for value in counts if float(value) >= 0]
    )
    chart = [hourly] * hours + [None] * (24 - hours) if expected is not None else [None] * 24
    assert daily["charts"]["steps"] == chart
    response = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert response.json()["charts"]["steps"] == chart


@pytest.mark.parametrize("zone_name,day,hours,shares", DST_DAYS)
@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT", "GARMIN"])
async def test_dense_platform_streams_keep_midnight_dst_and_selected_hours(
    sessions, monkeypatch, zone_name, day, hours, shares, preferred
):
    zone = ZoneInfo(zone_name)
    start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
    count = 64
    records = []
    for i in range(count):
        records.extend(
            (
                steps(f"band-{i}", hours, start, stop, BAND),
                steps(f"phone-{i}", 3, start + timedelta(hours=4), source=PHONE),
                steps(f"garmin-{i}", 5, start, source={"platform": "GARMIN"}),
                steps(
                    f"cross-{i}", 2, start - timedelta(minutes=30), start + timedelta(minutes=30)
                ),
            )
        )
    await store(sessions, "steps", *records)
    row, chart = await project(sessions, monkeypatch, preferred, day, zone.key)
    assert candidates(row, "steps") == {
        "FITBIT": count * (hours + 1),
        "HEALTH_KIT": count * 3,
        "GARMIN": count * 5,
    }
    expected = [None] * 24
    if preferred == "FITBIT":
        expected = [float(count)] * 24
        expected[0] += count
        for hour, share in shares.items():
            expected[hour] = None if share is None else count * share
    else:
        if preferred == "HEALTH_KIT":
            phone_start = (start + timedelta(hours=4)).astimezone(zone)
            tail = phone_start.minute / 60
            expected[phone_start.hour] = count * 3 * (1 - tail)
            if tail:
                expected[phone_start.hour + 1] = count * 3 * tail
        else:
            expected[0] = count * 5
    assert chart == expected


@pytest.mark.parametrize("zone_name", ["UTC", "Pacific/Chatham"])
async def test_dense_step_reader_memory_does_not_retain_each_contribution(
    sessions, monkeypatch, zone_name
):
    # A fixed small SQL batch isolates aggregation from JSON/driver buffering. Both
    # sizes exceed the batch, and input construction + ingestion precede tracing.
    monkeypatch.setattr(google_daily_readers, "_STEP_BATCH", 64)
    zone = ZoneInfo(zone_name)
    start = T0 if zone_name == "UTC" else datetime(2026, 9, 26, 13, 15, tzinfo=UTC)
    day = start.astimezone(zone).date()
    window = daily_window(day, 1, zone)
    calendar = CalendarRead(end_date=day, zone=zone, sources=DashboardSources())

    async def read():
        async with sessions() as session:
            reads = {}
            await google_daily_readers.read_steps(session, window, reads, calendar=calendar)
            assert not session.new and not session.dirty and not session.deleted
        return reads["FITBIT"]

    async def measure(count):
        gc.collect()
        tracemalloc.start()
        try:
            result = await read()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert result.days[day]["steps"].value == count
        expected = [None] * 24
        if zone_name == "UTC":
            expected[0] = float(count)
        else:
            expected[2:4] = [count * 0.75, count * 0.25]
        assert result.step_hours == expected
        return peak

    await store(sessions, "steps", *(steps(str(i), 1, start=start) for i in range(512)))
    await read()  # warm SQL compilation and eligibility caches outside measurement
    small = await measure(512)
    await store(sessions, "steps", *(steps(str(i), 1, start=start) for i in range(512, 8192)))
    large = await measure(8192)
    print(f"step read peak: 512 records={small} B; 8192 records={large} B")
    # Old daily/hourly lists retain about a megabyte more at the larger size.
    # Allow allocator/cache noise without a machine-dependent time threshold.
    assert large < small + 256 * 1024


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "counts,expected",
    [
        ([0.1] * 6, 1),
        ([120, -1, "-1e-1000"], 120),
        ([1e308, 1e308], None),
        (["5e-324"] * 2, 0),
    ],
)
async def test_transition_shares_keep_rounding_and_numeric_recovery(
    sessions, monkeypatch, reverse, counts, expected
):
    start = datetime(2026, 9, 26, 13, 15, tzinfo=UTC)
    records = [steps(str(i), value, start=start) for i, value in enumerate(counts)]
    await store_read_correction(sessions, "steps", *(reversed(records) if reverse else records))
    day = start.astimezone(ZoneInfo("Pacific/Chatham")).date()
    row, chart = await project(sessions, monkeypatch, day=day, zone="Pacific/Chatham")
    assert row["steps"] == expected
    shares = [
        previous_sum([(float(v), duration, 3600.0) for v in counts if float(v) >= 0])
        for duration in (2700, 900)
    ]
    assert chart == ([None] * 24 if expected is None else [None] * 2 + shares + [None] * 20)
