"""Track reads: shape-preserving thinning and reads bounded independently of samples."""

import gc
import json
import math
import tracemalloc
import zlib
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fit_factory import track_activity
from sqlalchemy import event, select
from test_garmin_track_api import garmin_workouts, upload

from app.models.health import ExerciseSession
from app.services.dashboard_views.dashboard_workouts import list_workouts
from app.services.providers import garmin_track_view as view
from app.services.providers.fit_track_columns import TrackFormatError, json_object
from app.services.providers.fit_track_thinning import sample_rows, thin
from app.services.providers.fit_tracks import SAMPLE_COLUMNS, Columns

CEILING = 2 * 1024 * 1024


def package(count, **channels):
    """A method 1 sample column set; ``channels`` map a name to ``index -> value``."""
    columns = Columns(SAMPLE_COLUMNS)
    for index in range(count):
        columns.append({"time": index, **{name: rule(index) for name, rule in channels.items()}})
    return columns.pack(0)


def test_lttb_keeps_a_heart_spike_and_a_route_corner():
    count, corner, spike = 5000, 3000, 1234
    step = 1e-6  # Inside the fictional box around 0°/0° (tests/fit_factory.py).
    blob = package(
        count,
        latitude=lambda index: step * min(index, corner),
        longitude=lambda index: step * max(index - corner, 0),
        heart_rate=lambda index: 190 if index == spike else 100,
        altitude=lambda index: None if index % 7 else 10.0,  # sparse channel
    )
    rows = thin(blob, count, [], 120)
    assert rows is not None and len(rows) <= 120 and rows == sorted(set(rows))
    assert {0, count - 1, corner, spike} <= set(rows)
    assert thin(blob, count, [], 120) == rows  # deterministic
    assert thin(blob, count, [], count) is None


def test_time_only_tracks_spread_and_boundaries_over_budget_are_spread():
    count = 1000
    blob = package(count)
    rows = thin(blob, count, [], 100)
    assert rows is not None and len(rows) == 100 and rows[0] == 0 and rows[-1] == count - 1
    boundaries = list(range(5, count - 1, 5))
    rows = thin(package(count, speed=lambda index: index % 3), count, boundaries, 100)
    assert rows is not None and len(rows) <= 100
    assert set(rows) <= {0, count - 1, *boundaries} and {0, count - 1} <= set(rows)


def test_lap_times_map_to_the_first_sample_at_or_after_them():
    blob = package(10, speed=lambda index: 1.0)
    assert sample_rows(blob, [0, 2.5, 3, 99, math.nan], 10) == {0: 0, 2.5: 3, 3: 3, 99: 9}
    assert sample_rows(blob, [1], 0) == {}


@pytest.mark.parametrize(
    "text",
    [
        b'{"time":[0,1],"speed":[1]}',
        b'{"time":[0,1x],"speed":[1,2]}',
        b'{"time":[0,1],"speed":[1,"private"]}',
        b'{"time":[0,1',
        b"not a package",
    ],
)
def test_damaged_package_fails_with_a_fixed_message(text):
    blob = text if text == b"not a package" else zlib.compress(text)

    def read():
        "".join(json_object(blob))
        thin(blob, 3, [], 2)  # Three rows claimed, two budgeted: both passes run.

    with pytest.raises(TrackFormatError) as caught:
        read()
    assert str(caught.value) == "İz paketi okunamadı / Track package unreadable"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("count", [2000, 20000])
async def test_track_reads_and_memory_do_not_grow_with_the_sample_count(sessions, count):
    await upload(sessions, track_activity(count, seed=5, splits=(100, 900)))
    async with sessions() as session:
        workout = await session.scalar(select(ExerciseSession.id))
    statements = []

    def trace(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = sessions.kw["bind"].sync_engine
    async with sessions() as session:
        await view.workout_track(session, workout)  # warm SQL caches outside measurement
        event.listen(engine, "before_cursor_execute", trace)
        try:
            track = await view.workout_track(session, workout)
        finally:
            event.remove(engine, "before_cursor_execute", trace)
    assert track is not None and len(statements) == 4  # row, listing, file or link, package
    for max_points in (500, None):
        gc.collect()
        tracemalloc.start()
        try:
            plan = view.plan_track(track.stored, max_points)
            size = sum(len(chunk) for chunk in view.track_document(track, plan))
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak - retained < CEILING, (max_points, peak)
        assert size > 0
    # The ceiling discriminates: decoding the column set into Python lists does not fit.
    tracemalloc.start()
    try:
        decoded = json.loads(zlib.decompress(track.stored.samples))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(decoded["time"]) == count
    assert (peak > CEILING) == (count == 20000)


async def test_listing_has_track_costs_a_fixed_number_of_queries(sessions):
    counts = []
    for keys in ((1,), (2, 3, 4)):
        await garmin_workouts(sessions, {key: track_activity(10, seed=key) for key in keys})
        statements = []

        def trace(conn, cursor, statement, parameters, context, many, found=statements):
            found.append(statement)

        engine = sessions.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", trace)
        try:
            async with sessions() as session:
                rows = await list_workouts(session, date(2026, 11, 1), ZoneInfo("UTC"))
        finally:
            event.remove(engine, "before_cursor_execute", trace)
        assert [row["has_track"] for row in rows] == [True] * len(rows)
        counts.append(len(statements))
    assert counts[0] == counts[1]
