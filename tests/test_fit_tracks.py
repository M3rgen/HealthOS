"""Only generated, ocean-origin geometry is used for track projection tests."""

import json
import math
import struct
import tracemalloc
import zlib

import pytest
from fit_factory import (
    STAMP,
    START,
    TRACK_BOUNDS,
    definition,
    fit_file,
    track_activity,
    track_messages,
    track_points,
)

from app.services.providers.fit_decode import FitError
from app.services.providers.fit_track_summary import TrackSummary
from app.services.providers.fit_track_values import (
    RECORD_FIELDS,
    lap,
    measurements,
    sample,
    timer_event,
)
from app.services.providers.fit_tracks import derive_track


def columns(blob):
    return json.loads(zlib.decompress(blob))


def rows(blob):
    """Columns -> per-record dicts without missing values (test convenience only)."""
    data = columns(blob)
    lengths = {len(values) for values in data.values()}
    assert len(lengths) == 1, "columns must have equal length"
    return [
        {name: values[index] for name, values in data.items() if values[index] is not None}
        for index in range(lengths.pop())
    ]


@pytest.mark.parametrize("enhanced", [True, False])
@pytest.mark.parametrize("optional", [True, False])
@pytest.mark.parametrize("endian", ["<", ">"])
def test_fields_units_laps_and_optional_channels(enhanced, optional, endian):
    points = list(track_points(3, intervals=[4]))
    package = derive_track(
        fit_file(track_messages(points, enhanced=enhanced, optional=optional, endian=endian))
    )
    samples = rows(package.samples)
    assert len(samples) == package.sample_count == 3
    assert package.summary["start_time"] == START.isoformat()
    assert ("cadence" in columns(package.samples)) == ("power" in columns(package.samples))
    assert ("cadence" in columns(package.samples)) == optional  # absent channel: no array
    for expected, actual in zip(points, samples, strict=True):
        assert actual["time"] == expected["time"]
        for name in ("latitude", "longitude"):
            assert actual[name] == pytest.approx(expected[name], abs=1e-7)
        for name in ("speed", "altitude", "heart_rate", "distance", "temperature"):
            assert actual[name] == expected[name]
        if optional:
            assert (actual["cadence"], actual["power"]) == (80, 200)
    assert rows(package.laps) == [
        {
            "start_time": 0,
            "time": 8,
            "elapsed_seconds": 8,
            "moving_seconds": 8,
            "distance": 96,
            "average_speed": 12,
            "maximum_speed": 12,
            "average_heart_rate": 125,
            "maximum_heart_rate": 139,
        }
    ]


@pytest.mark.parametrize(
    "overrides, missing",
    [
        ({0: 0x7FFFFFFF}, ("latitude", "longitude")),  # a position is a pair
        ({1: 0x7FFFFFFF}, ("latitude", "longitude")),
        ({0: 2**30 + 1}, ("latitude", "longitude")),  # |latitude| > 90
        ({1: -(2**31) + 1}, ()),  # -180 degrees is valid
        ({0: 0, 1: 0}, ("latitude", "longitude")),  # wire (0, 0): "no fix"
        ({3: 255}, ("heart_rate",)),
        ({3: 0}, ("heart_rate",)),
        ({4: 255}, ("cadence",)),
        ({5: 0xFFFFFFFF}, ("distance",)),
        ({73: 0xFFFFFFFF}, ("speed",)),
        ({78: 0xFFFFFFFF}, ("altitude",)),
        ({7: 0xFFFF}, ("power",)),
        ({13: 127}, ("temperature",)),
    ],
)
def test_invalid_wire_measurements_are_omitted_and_neighbors_survive(overrides, missing):
    points = rows(derive_track(track_activity(3, overrides={1: overrides})).samples)
    assert len(points) == 3
    for name in missing or ("latitude",):
        assert name in points[0] and name in points[2]
        assert (name in points[1]) == (not missing)
    assert {"time", "heart_rate", "speed"} - set(missing) <= set(points[1])


def test_positions_near_zero_are_kept_and_generator_never_emits_zero_pair():
    assert sample({253: STAMP, 0: 0, 1: 1}) == {
        "time": STAMP,
        "latitude": 0,
        "longitude": 1e-7,
    }
    assert sample({253: STAMP, 0: -1, 1: 0})["latitude"] == -1e-7
    for seed in range(5):
        for point in track_points(400, seed=seed):
            wire = (round(point[name] * 2**31 / 180) for name in ("latitude", "longitude"))
            assert tuple(wire) != (0, 0)
            assert TRACK_BOUNDS[0] <= point["latitude"] <= TRACK_BOUNDS[2]
            assert TRACK_BOUNDS[1] <= point["longitude"] <= TRACK_BOUNDS[3]


@pytest.mark.parametrize("field", RECORD_FIELDS)
@pytest.mark.parametrize("invalid", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_measurements_are_omitted(field, invalid):
    assert measurements({field: invalid}, RECORD_FIELDS) == {}


@pytest.mark.parametrize(
    "field,value", [(73, -1), (6, -1), (5, -1), (7, -1), (4, -1), (1, 2**31 + 1)]
)
def test_negative_or_out_of_range_values_are_omitted(field, value):
    assert measurements({field: value}, RECORD_FIELDS) == {}


@pytest.mark.parametrize("field", [5, 6, 73, 78])
@pytest.mark.parametrize("invalid", [-5e-324, 1e308])
def test_scaling_cannot_hide_negative_or_out_of_native_range_values(field, invalid):
    assert measurements({field: invalid}, RECORD_FIELDS) == {}


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan")])
def test_float_wire_speed_is_invalid_without_rejecting_file(value):
    messages = definition(2, 20, [(253, 4, 0x86), (73, 4, 0x88)])
    messages += b"".join(
        b"\x02" + struct.pack("<If", STAMP + i, speed)
        for i, speed in enumerate((12000, value, 12000))
    )
    assert columns(derive_track(fit_file(messages)).samples) == {
        "time": [0, 1, 2],
        "speed": [12, None, 12],
    }


def test_invalid_timestamp_drops_only_that_record_and_enhanced_falls_back():
    package = derive_track(track_activity(3, overrides={1: {253: 0xFFFFFFFF}}))
    assert columns(package.samples)["time"] == [0, list(track_points(3))[-1]["time"]]
    point = sample({253: STAMP, 6: 5000, 73: -1, 2: 2600, 78: float("nan")})
    assert point == {"time": STAMP, "speed": 5, "altitude": 20}
    # FIT altitude: raw / 5 - 500 m; enhanced wins when valid.
    assert sample({253: STAMP, 2: 2600, 78: 2505})["altitude"] == 1


def test_sparse_intervals_and_pauses_use_seconds_and_odometer():
    pauses = [(7, 33), (60, 70)]
    data = track_activity(12, intervals=[1, 4, 25, 10], pauses=pauses)
    package = derive_track(data)
    expected = list(track_points(12, intervals=[1, 4, 25, 10], pauses=pauses))[-1]
    summary = package.summary
    assert summary["elapsed_seconds"] == expected["time"]
    assert summary["moving_seconds"] == expected["time"] - 36
    assert summary["distance"] == expected["distance"]
    assert summary["average_speed"] == summary["maximum_speed"] == 12
    events = rows(package.events)
    assert [e["action"] for e in events] == ["pause", "resume", "pause", "resume"]
    assert [e["time"] for e in events] == [7, 33, 60, 70]
    assert rows(package.laps)[0]["moving_seconds"] == summary["moving_seconds"]


@pytest.mark.parametrize("intervals", [[1], [4], [1, 4, 25, 10], None])
def test_coordinate_distance_matches_odometer_without_distance_field(intervals):
    pauses = [(7, 33)]
    overrides = {i: {5: 0xFFFFFFFF} for i in range(300)}
    with_odometer = derive_track(track_activity(300, intervals=intervals, pauses=pauses)).summary
    summary = derive_track(
        track_activity(300, intervals=intervals, pauses=pauses, overrides=overrides)
    ).summary
    # Haversine chords of a 556 m circle: below the arc by at most 1.2 % at 25 s steps.
    assert summary["distance"] == pytest.approx(with_odometer["distance"], rel=0.012)
    assert summary["distance"] <= with_odometer["distance"] * 1.0001


def test_missing_odometer_and_coordinates_fall_back_to_timed_speed_with_pauses():
    pauses = [(7, 33)]
    overrides = {i: {0: 0x7FFFFFFF, 1: 0x7FFFFFFF, 5: 0xFFFFFFFF} for i in range(12)}
    package = derive_track(
        track_activity(12, intervals=[1, 4, 25, 10], pauses=pauses, overrides=overrides)
    )
    summary = package.summary
    assert summary["distance"] == summary["moving_seconds"] * 12
    assert summary["bounds"] is None
    assert not {"latitude", "longitude", "distance"} & columns(package.samples).keys()


def test_seed_determinism_bounds_and_elevation_threshold():
    first = track_activity(81, seed=9)
    assert first == track_activity(81, seed=9) and first != track_activity(81, seed=10)
    result = derive_track(first)
    assert result == derive_track(first)
    south, west, north, east = result.summary["bounds"]
    assert TRACK_BOUNDS[:2] <= (south, west) and (north, east) <= TRACK_BOUNDS[2:]
    assert north - south > 0.0099 and east - west > 0.0099  # the whole circle
    # 1 m noise alone contributes nothing; cumulative changes crossing 3 m do.
    points = list(track_points(5))
    for point, altitude in zip(points, (10, 11, 10, 14, 10), strict=True):
        point["altitude"] = altitude
    summary = derive_track(fit_file(track_messages(points))).summary
    assert summary["ascent"] == summary["descent"] == 4


def test_unknown_native_and_developer_messages_do_not_change_projection():
    original = track_messages(track_points(5))
    extra = definition(5, 999, [(99, 3, 13)], developer=[(0, 2, 0)]) + b"\x05abcde"
    assert derive_track(fit_file(original)) == derive_track(fit_file(original + extra))


def test_damaged_tail_never_returns_partial_projection():
    data = track_messages(track_points(5)) + definition(5, 999, [(0, 4, 0x86)]) + b"\x05"
    with pytest.raises(FitError):
        derive_track(fit_file(data))


def test_empty_and_channel_free_tracks_have_null_summaries():
    empty = derive_track(fit_file(b""))
    assert (empty.sample_count, empty.summary["start_time"]) == (0, None)
    assert columns(empty.samples) == {"time": []} and columns(empty.laps) == {
        "start_time": [],
        "time": [],
    }
    bare = derive_track(
        fit_file(definition(2, 20, [(253, 4, 0x86)]) + b"\x02" + struct.pack("<I", STAMP))
    )
    assert bare.summary["maximum_speed"] is bare.summary["ascent"] is bare.summary["bounds"] is None


@pytest.mark.timeout(300)
def test_twenty_thousand_records_have_bounded_memory():
    # Generate before tracing, as in the existing archive-reader memory tests.
    data = track_activity(20_000)
    tracemalloc.start()
    try:
        package = derive_track(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert package.sample_count == 20_000
    # Decoded row dicts alone would need about 20 MB; columns need 1.6 MB.
    assert peak < 4 * 1024 * 1024
    print(
        f"records={package.sample_count} bytes={len(data)} "
        f"packed={len(package.samples)} peak={peak}"
    )


def test_enhanced_lap_speed_and_all_timer_stop_variants():
    assert lap({2: STAMP, 7: 8000, 13: 3000, 14: 4000, 110: 5000, 111: 6000}) == {
        "start_time": STAMP,
        "elapsed_seconds": 8,
        "average_speed": 5,
        "maximum_speed": 6,
    }
    for action in (1, 4, 8, 9):
        assert timer_event({253: STAMP, 0: 0, 1: action}) == {"time": STAMP, "action": "pause"}
    for fields in ({253: STAMP, 0: 1, 1: 0}, {253: STAMP, 0: 0, 1: 255}):
        assert timer_event(fields) is None


def test_backward_records_and_late_events_do_not_overcount_the_interval():
    summary = TrackSummary()
    assert summary.sample({"time": 0, "speed": 1})
    summary.event({"time": 10, "action": "pause"})
    assert summary.sample({"time": 5, "speed": 1})  # behind the clock, kept, adds no time
    assert summary.sample({"time": 20, "speed": 1})
    assert not summary.sample({"time": 20, "speed": 1})
    assert not summary.sample({"time": 15, "speed": 1})
    summary.event({"time": 12, "action": "resume"})  # late: resumes from the clock (20)
    assert summary.sample({"time": 30, "speed": 1})
    assert summary.result()["moving_seconds"] == 20
    assert summary.result()["elapsed_seconds"] == 30


def test_ground_distance_matches_a_known_equatorial_arc():
    from app.services.providers.fit_track_summary import ground_distance

    first, second = {"latitude": 0, "longitude": 0.001}, {"latitude": 0, "longitude": 0.002}
    assert ground_distance(first, second) == pytest.approx(math.radians(0.001) * 6_371_000)
    assert ground_distance(first, {"latitude": 0}) is None
