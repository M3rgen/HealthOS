"""Session indexing agrees with the former all-pairs resolver, with bounded work."""

import random
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

import app.api.dashboard as api
from app.db.session import get_session
from app.google_health.data_types import REGISTRY
from app.models.manual import ManualWorkout
from app.services.archive.storage import store_records
from app.services.dashboard_views import dashboard_workouts
from app.services.identities import source_precedence as sp

T = datetime(2026, 8, 1, tzinfo=UTC)
POLICIES = [sp.Policy(), sp.Policy(activity=("FITBIT", "GARMIN")), sp.Policy(activity=())]


def session(key, source, start, end):
    return sp.Observation(
        source,
        {"id": key},
        record_id=key,
        start=T + timedelta(seconds=start),
        end=T + timedelta(seconds=end),
    )


def reference(sessions, policy):
    """Independent copy of the old graph/placement contract; no index helpers."""
    items = {item.record_id: item for item in sessions if item.record_id is not None}
    parent = {key: key for key in items}

    def root(key):
        while parent[key] != key:
            key = parent[key]
        return key

    def order(key):
        return items[key].start or datetime.min, key

    def overlaps(left, right):
        a, b = items[left], items[right]
        return (
            a.start is not None
            and a.end is not None
            and b.start is not None
            and b.end is not None
            and a.start < b.end
            and b.start < a.end
        )

    devices = sorted((key for key, item in items.items() if item.source != sp.MANUAL), key=order)
    for i, left in enumerate(devices):
        for right in devices[i + 1 :]:
            if items[left].source != items[right].source and overlaps(left, right):
                parent[root(right)] = root(left)
    groups = {}
    for key in devices:
        groups.setdefault(root(key), []).append(key)
    placed = {
        key: sp.SessionPlacement(sp.PRIMARY)
        for key, item in items.items()
        if item.source == sp.MANUAL
    }
    for members in groups.values():
        ranks = {key: policy.rank(items[key].source, sp.ACTIVITY) for key in members}
        best = min(ranks.values())
        primaries = sorted((key for key in members if ranks[key] == best), key=order)
        held = {key: [] for key in primaries}
        for key in sorted((key for key in members if ranks[key] != best), key=order):
            holder = next((p for p in primaries if overlaps(p, key)), primaries[0])
            held[holder].append(items[key])
            placed[key] = sp.SessionPlacement(sp.SECONDARY, holder)
        for key in primaries:
            placed[key] = sp.SessionPlacement(sp.PRIMARY, None, tuple(held[key]))
    return placed


def assert_reference(sessions, policy):
    expected = reference(sessions, policy)
    actual = sp.resolve_sessions(sessions, policy)
    assert actual == expected  # Includes every alternative observation and its order.
    assert list(actual) == list(expected)
    items = {item.record_id: item for item in sessions if item.record_id is not None}
    assert set(actual) == set(items)
    represented = [key for key, placement in actual.items() if placement.priority == sp.PRIMARY] + [
        item.record_id for placement in actual.values() for item in placement.alternatives
    ]
    assert sorted(represented) == sorted(items)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize(
    "spec",
    [
        [],
        [("g", "GARMIN", 0, 5), ("f", "FITBIT", 4, 12), ("h", "HEALTH_KIT", 11, 20)],
        [("a", "GARMIN", 0, 8), ("b", "GARMIN", 7, 15), ("f", "FITBIT", 14, 20)],
        [("a", "GARMIN", 0, 10), ("b", "FITBIT", 10, 20), ("c", "HEALTH_KIT", 20, 30)],
        [("z", "GARMIN", 0, 30), ("a", "GARMIN", 0, 10), ("f", "FITBIT", 2, 8)],
        [("a", "GARMIN", 0, 100), ("b", "FITBIT", 2, 90), ("c", "HEALTH_KIT", 3, 80)],
        [("b", "WHOOP", 0, 30), ("a", "OTHER", 0, 20), ("f", "FIT", 3, 10)],
        [("g", "GARMIN", 0, 30), ("m", "MANUAL", 0, 40), ("f", "FITBIT", 35, 45)],
        [("", "GARMIN", 0, 20), ("b", "GARMIN", 1, 10), ("f", "FITBIT", 2, 8)],
    ],
)
def test_session_shapes_keep_the_complete_placement_contract(spec, policy):
    observations = [session(*row) for row in spec]
    assert_reference(observations, policy)
    assert_reference(list(reversed(observations)), policy)


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("seed", range(30))
def test_small_synthetic_graphs_match_all_pairs(seed, policy):
    rng = random.Random(seed)
    observations = []
    for i in range(40):
        source = rng.choice(["GARMIN", "FITBIT", "HEALTH_KIT", "OTHER", "WHOOP", "FIT", "MANUAL"])
        start = rng.randrange(25)
        observations.append(session(str(i), source, start, start + rng.randrange(1, 15)))
    rng.shuffle(observations)
    assert_reference(observations, policy)


@pytest.mark.parametrize("policy", POLICIES)
def test_missing_bounds_ids_and_degenerate_intervals_keep_legacy_behavior(policy):
    observations = [
        session("g", "GARMIN", 0, 30),
        session("zero", "FITBIT", 10, 10),
        session("reverse", "HEALTH_KIT", 20, 15),
        session("edge", "FITBIT", 30, 30),
        session("missing-end", "FITBIT", 5, 15),
        session("missing-start", "FITBIT", 5, 15),
        session("duplicate", "GARMIN", 100, 110),
        session("duplicate", "FITBIT", 5, 15),
        session(None, "GARMIN", 0, 30),
    ]
    # Naive bounds also exercise the existing datetime.min ordering of missing starts.
    observations = [
        replace(item, start=item.start.replace(tzinfo=None), end=item.end.replace(tzinfo=None))
        for item in observations
    ]
    observations[4] = replace(observations[4], end=None)
    observations[5] = replace(observations[5], start=None)
    assert_reference(observations, policy)


def counted_resolution(observations):
    """Count executed production lines, including source checks and holder searches.

    Unlike counting only overlap calls, this catches same-source pair scans too.
    The ceiling stops a quadratic regression early; no wall-clock assertion is used.
    """
    count = 0
    limit = 100 * len(observations) * len(observations).bit_length()

    def trace(frame, event, arg):
        nonlocal count
        if frame.f_code.co_filename != sp.__file__:
            return None
        if event == "line":
            count += 1
            assert count <= limit, "session resolution exceeded the O(n log n) work ceiling"
        return trace

    previous = sys.gettrace()
    try:
        sys.settrace(trace)
        placed = sp.resolve_sessions(observations, POLICIES[0])
    finally:
        sys.settrace(previous)
    return placed, count


@pytest.mark.parametrize("shape", ["disjoint", "single-source", "dense-mixed", "holders"])
def test_large_session_work_grows_subquadratically(shape):
    counts = []
    for size in (1000, 10000):
        if shape == "disjoint":
            observations = [
                session(str(i), "GARMIN" if i % 2 else "FITBIT", 2 * i, 2 * i + 1)
                for i in range(size)
            ]
        elif shape == "single-source":
            observations = [session(str(i), "GARMIN", 0, size + i) for i in range(size)]
        elif shape == "dense-mixed":
            observations = [
                session(str(i), "GARMIN" if i % 2 else "FITBIT", 0, size + i) for i in range(size)
            ]
        else:
            # One lower-ranked bridge joins disjoint primaries. Each short alternative
            # belongs to a later primary: a linear holder search would be quadratic.
            observations = [session("bridge", "HEALTH_KIT", 0, 2 * size)]
            for i in range(size // 2):
                observations.extend(
                    [
                        session(f"g{i}", "GARMIN", 3 * i, 3 * i + 2),
                        session(f"f{i}", "FITBIT", 3 * i + 1, 3 * i + 2),
                    ]
                )
        placed, count = counted_resolution(observations)
        counts.append(count)
        assert len(placed) == len(observations)
        if shape in ("disjoint", "single-source"):
            assert all(p == sp.SessionPlacement(sp.PRIMARY) for p in placed.values())
        elif shape == "holders":
            assert placed["bridge"].group == "g0"
            assert all(placed[f"f{i}"].group == f"g{i}" for i in range(size // 2))
        else:
            assert sum(p.priority == sp.PRIMARY for p in placed.values()) == size // 2
            assert sum(len(p.alternatives) for p in placed.values()) == size // 2
    assert counts[1] <= 16 * counts[0]  # 10x input permits log growth, not 100x pair scans.


@pytest.mark.parametrize("policy", POLICIES)
async def test_monthly_workout_api_preserves_indexed_placements(sessions, monkeypatch, policy):
    observations = [
        session("g1", "GARMIN", 0, 10),
        session("g2", "GARMIN", 0, 20),
        session("f1", "FITBIT", 5, 15),
        session("f2", "FITBIT", 14, 30),
        session("h1", "HEALTH_KIT", 29, 40),  # Transitive; no overlapping Garmin.
        session("g3", "GARMIN", 40, 50),  # Half-open boundary, its own group.
        session("x", "OTHER", 60, 80),
        session("w", "WHOOP", 60, 90),  # Equal rank, both primary.
        session("fit", "FIT", 65, 70),
    ]
    async with sessions.begin() as db:
        for item in observations:
            await store_records(
                db,
                REGISTRY["exercise"],
                [
                    {
                        "name": f"synthetic/workout-index/{item.record_id}",
                        "dataSource": {"platform": item.source},
                        "exercise": {
                            "interval": {
                                "startTime": item.start.isoformat(),
                                "endTime": item.end.isoformat(),
                            },
                            "exerciseType": "RUNNING",
                            "activeDuration": "5s",
                        },
                    }
                ],
            )
        db.add(
            ManualWorkout(exercise_type="Yürüyüş", start_time=T, end_time=T + timedelta(seconds=90))
        )

    captured = []
    original = dashboard_workouts.place_workouts

    def place(rows):
        captured.extend(deepcopy(rows))
        return original(rows, policy)

    monkeypatch.setattr(dashboard_workouts, "place_workouts", place)
    application = FastAPI()
    application.include_router(api.router)

    async def override_session():
        async with sessions() as db:
            yield db

    application.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url="http://test"
    ) as client:
        response = await client.get("/api/dashboard/workouts?month=2026-08-01&timezone=UTC")
    assert response.status_code == 200, response.text
    assert len(captured) == len(observations) + 1
    assert [row["id"] for row in response.json()] == [
        row["id"] for row in sorted(captured, key=lambda row: row["start_time"], reverse=True)
    ]
    items = {
        f"{row['source_code']}:{row['id']}": sp.Observation(
            row["source_code"],
            row,
            record_id=f"{row['source_code']}:{row['id']}",
            start=row["start_time"],
            end=row["end_time"],
        )
        for row in captured
    }
    expected = []
    for key, placement in reference(list(items.values()), policy).items():
        row = items[key].value
        expected.append(
            {
                **row,
                "source": sp.label(row["source_code"]),
                "priority": placement.priority,
                "group": items[placement.group].value["id"]
                if placement.group is not None
                else None,
                "alternatives": [
                    {
                        "id": item.value["id"],
                        "source": sp.label(item.source),
                        "source_code": item.source,
                        "start_time": item.start,
                        "end_time": item.end,
                    }
                    for item in placement.alternatives
                ],
            }
        )
    assert sorted(response.json(), key=lambda row: row["id"]) == jsonable_encoder(
        sorted(expected, key=lambda row: row["id"]),
        custom_encoder={datetime: lambda value: value.isoformat().replace("+00:00", "Z")},
    )
