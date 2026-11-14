"""UTC transition boundaries survive ingestion and the 24 civil-hour projection."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import islice
from zoneinfo import ZoneInfo

import pytest
from derived_records import PHONE, steps, store
from test_google_platform_daily import candidates, project

from app.services.readers.measurement_windows import StepWindow, step_window

TRANSITIONS = (
    ("Pacific/Chatham", "2026-09-26T14:00:00Z", 2, 3),
    ("Pacific/Chatham", "2026-04-04T14:00:00Z", 3, 2),
    ("Australia/Lord_Howe", "2026-10-03T15:30:00Z", 1, 2),
    ("Australia/Lord_Howe", "2026-04-04T15:00:00Z", 1, 1),
    ("Europe/Brussels", "2026-03-29T01:00:00Z", 1, 3),
    ("Europe/Brussels", "2026-10-25T01:00:00Z", 2, 2),
)


@pytest.mark.parametrize("zone_name,instant,before,after", TRANSITIONS)
@pytest.mark.parametrize("left,right", [(-1, 1), (-1, 0), (0, 1), (-1000001, 1000001)])
def test_clipped_transition_edges_progress_at_microsecond_resolution(
    zone_name, instant, before, after, left, right
):
    transition = datetime.fromisoformat(instant)
    start = transition + timedelta(microseconds=left)
    stop = transition + timedelta(microseconds=right)
    record = steps(start=transition - timedelta(hours=1), end=transition + timedelta(hours=1))
    allocation = step_window(record, start, stop)
    assert allocation is not None
    # Bound iteration explicitly: a stuck microsecond edge fails without hanging.
    terms = list(islice(allocation.hourly_terms(ZoneInfo(zone_name)), 4))
    assert 1 <= len(terms) <= 2
    expected = defaultdict(float)
    if left < 0:
        expected[before] += -left / 1e6
    if right > 0:
        expected[after] += right / 1e6
    durations = defaultdict(float)
    for local, (count, duration, divisor) in terms:
        assert count == 120 and divisor == 7200 and duration > 0
        durations[local.hour] += duration
    assert dict(durations) == dict(expected)
    assert sum(durations.values()) == (stop - start).total_seconds()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("preferred", ["FITBIT", "HEALTH_KIT"])
@pytest.mark.parametrize("count", [600, 0])
@pytest.mark.parametrize("cuts", [(0, 1), (0, 0.75, 1), (0, 0.5, 1)])
async def test_chatham_600_steps_reach_selected_dashboard_without_archive_writes(
    sessions, monkeypatch, reverse, preferred, count, cuts
):
    start = datetime(2026, 9, 26, 13, 15, tzinfo=UTC)
    stop = start + timedelta(hours=1)
    records = [
        *(
            steps(
                f"band-{a}", count * (b - a), start + timedelta(hours=a), start + timedelta(hours=b)
            )
            for a, b in zip(cuts, cuts[1:], strict=False)
        ),
        steps("phone", 200, start, stop, PHONE),
        steps("zero", 0, stop, stop + timedelta(hours=1)),
    ]
    await store(sessions, "steps", *(reversed(records) if reverse else records))
    day = start.astimezone(ZoneInfo("Pacific/Chatham")).date()
    row, chart = await project(sessions, monkeypatch, preferred, day, "Pacific/Chatham")
    assert candidates(row, "steps") == {"FITBIT": count, "HEALTH_KIT": 200}
    expected = [None] * 24
    expected[2:4] = [count * 0.75, count * 0.25] if preferred == "FITBIT" else [150.0, 50.0]
    if preferred == "FITBIT":
        expected[4] = 0.0
    assert chart == expected
    assert row["steps"] == (count if preferred == "FITBIT" else 200)


@pytest.mark.parametrize("zone_name", ["UTC", "Asia/Kathmandu", "Africa/Monrovia"])
def test_fractional_offsets_and_microsecond_hour_edges(zone_name):
    zone = ZoneInfo(zone_name)
    # Monrovia's historical offset has seconds; Kathmandu's has 45 minutes.
    wall = datetime(1970 if zone_name == "Africa/Monrovia" else 2000, 1, 2, 1, tzinfo=zone)
    edge = wall.astimezone(UTC)
    start, stop = edge - timedelta(microseconds=1), edge + timedelta(microseconds=1)
    allocation = StepWindow(start, stop, 2, start, stop)
    terms = list(islice(allocation.hourly_terms(zone), 4))
    assert [(local.hour, term) for local, term in terms] == [
        (0, (2, 1e-6, 2e-6)),
        (1, (2, 1e-6, 2e-6)),
    ]


def test_clipped_datetime_limit_does_not_add_beyond_stop():
    stop = datetime.max.replace(tzinfo=UTC)
    start = stop - timedelta(microseconds=1)
    terms = list(StepWindow(start, stop, 1, start, stop).hourly_terms(ZoneInfo("UTC")))
    assert len(terms) == 1 and terms[0][1] == (1, 1e-6, 1e-6)


def test_hour_split_probes_are_bounded_without_scanning_seconds():
    probes = []

    class Probe(datetime):
        def astimezone(self, zone):
            probes.append(self)
            return super().astimezone(zone)

    start = Probe(2026, 9, 26, 13, 15, tzinfo=UTC)
    stop = start + timedelta(hours=1)
    terms = list(
        StepWindow(start, stop, 600, start, stop).hourly_terms(ZoneInfo("Pacific/Chatham"))
    )
    assert len(terms) == 2
    assert len(probes) < 40
