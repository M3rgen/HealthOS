"""Step archive bounds from windows at the archive's ends equal the full sweep's.

``reference_bounds`` is the full read as it was before the windows (one statement over every
step row of the archive), frozen here. Every archive is synthetic: generated intervals and
counts around the shared fixture day, never a recorded measurement.
"""

import random
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from derived_records import BAND, DAY, PHONE, T0, snapshot, steps
from numeric_records import store_read_correction
from sqlalchemy import select, update

from app.models.eligibility import raw_counted
from app.models.health import Steps
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.services.interpretation import google_daily_readers as reader
from app.services.interpretation.step_totals import StepSweep
from app.services.readers import checked_dates, daily_window
from app.services.readers.reading import ArchiveBounds, DailyValue

ZONES = (
    "UTC",
    "Europe/Brussels",
    "America/St_Johns",
    "Pacific/Kiritimati",
    "Pacific/Pago_Pago",
    "Australia/Lord_Howe",
)


async def _reference_days(session, today, zone):
    """The pre-window ``_step_days``: one ordered read of every candidate."""
    start = datetime.min.replace(tzinfo=UTC)
    stop = daily_window(today, 1, zone).stop
    records = await session.stream_scalars(
        select(RawHealthData.raw_json)
        .where(
            RawHealthData.data_type == "steps",
            raw_counted(data_types=("steps",)),
            RawHealthData.source_start_time < stop,
            RawHealthData.source_end_time > RawHealthData.source_start_time,
            select(Steps.id)
            .where(
                Steps.google_data_point_id == RawHealthData.google_data_point_id,
                Steps.is_quarantined.is_(False),
            )
            .exists(),
        )
        .order_by(RawHealthData.source_start_time, RawHealthData.google_data_point_id),
        execution_options={"yield_per": 1000},
    )
    sweep = StepSweep(zone)
    try:
        with checked_dates():
            async for record in records:
                allocation = reader.step_window(record, start, stop)
                if allocation is None:
                    continue
                for day, source, value in sweep.advance(allocation.start):
                    yield day, DailyValue(value, source, "GOOGLE")
                sweep.add(reader._platform_source(record), allocation)
    finally:
        await records.close()
    with checked_dates():
        for day, source, value in sweep.advance(None):
            yield day, DailyValue(value, source, "GOOGLE")


async def reference_bounds(session, today, zone):
    """The pre-window ``_step_archive_bounds``."""
    earliest = latest = None
    async with aclosing(_reference_days(session, today, zone)) as days:
        async for day, measurement in days:
            if measurement.value is not None:
                earliest = min(earliest, day) if earliest is not None else day
                latest = max(latest, day) if latest is not None else day
    return ArchiveBounds(earliest, latest)


# Counts: ordinary, measured zero and underflow; overflow (two on a platform's day exceed
# float); payloads the reader rejects (negative, non-numeric, missing).
MEASURED = (lambda rng: rng.randrange(1, 4000), lambda rng: 0, lambda rng: "5e-324")
REJECTED = (lambda rng: -1, lambda rng: "synthetic", lambda rng: None)
LENGTHS = (
    [lambda rng: timedelta(minutes=rng.randrange(1, 60))] * 6
    + [lambda rng: timedelta(hours=rng.randrange(1, 30))] * 2
    + [lambda rng: timedelta(days=rng.randrange(1, 5), minutes=rng.randrange(60))]
)


def generated(rng, name, around, shape):
    rejected, overflowing = shape
    if rng.random() < 0.3:  # near a UTC midnight, where every window edge lies
        around = around.replace(hour=0, minute=0) + timedelta(minutes=rng.randrange(-180, 180))
    start = around + timedelta(minutes=rng.randrange(-36 * 60, 36 * 60))
    draw = rng.random()
    count = (
        rng.choice(REJECTED)(rng)
        if draw < rejected
        else 1e308
        if draw < rejected + overflowing
        else rng.choice(MEASURED)(rng)
    )
    record = steps(
        name, count, start, start + rng.choice(LENGTHS)(rng), source=rng.choice((BAND, PHONE))
    )
    if record["steps"]["count"] is None:
        del record["steps"]["count"]
    return record


def archive(rng):
    """Clusters of intervals across up to ~14 months, some after the read's stop.

    The shares of rejected and overflowing counts vary per archive, so valid days range
    from dense to a few isolated ones that both ends must widen to reach.
    """
    shape = (rng.choice((0.05, 0.5, 0.85)), rng.choice((0.0, 0.2, 0.45)))
    centres = [T0 - timedelta(days=rng.randrange(-3, 420)) for _ in range(rng.randrange(1, 8))]
    records = [
        generated(rng, f"g{index}", rng.choice(centres), shape)
        for index in range(rng.randrange(1, 70))
    ]
    return records, shape


async def mutate(sessions, rng, records, shape):
    """Quarantine, delete or supersede some rows, and correct others in place."""
    names = [record["name"] for record in records]
    async with sessions.begin() as session:
        rows = {
            row.google_data_point_id: row
            for row in (await session.scalars(select(RawHealthData))).all()
        }
        for name in names:
            action = rng.randrange(14)
            row = rows[name]
            if action == 0:
                row.is_quarantined = True
            elif action == 1:
                row.source_deleted_at = T0
            elif action == 2:
                await session.execute(
                    update(Steps)
                    .where(Steps.google_data_point_id == name)
                    .values(is_quarantined=True)
                )
            elif action == 3 and len(names) > 1:
                holder = rows[rng.choice([other for other in names if other != name])]
                if holder.source_deleted_at is None and not holder.is_quarantined:
                    session.add(
                        RawHealthDataSupersession(
                            old_raw_id=row.id,
                            new_raw_id=holder.id,
                            data_type="steps",
                            old_google_data_point_id=row.google_data_point_id,
                            new_google_data_point_id=holder.google_data_point_id,
                            reason="duplicate",
                            old_fingerprint="a" * 64,
                            new_fingerprint="a" * 64,
                            same_reading=True,
                            new_payload_hash=holder.payload_hash,
                        )
                    )
    corrections = []
    for record in records:
        if rng.randrange(8) == 0:
            moved = generated(rng, record["name"].rsplit("/", 1)[1], T0, shape)
            # Keep the correction near the original, or move it across the archive.
            if rng.randrange(2):
                original = datetime.fromisoformat(record["steps"]["interval"]["startTime"])
                moved = generated(rng, record["name"].rsplit("/", 1)[1], original, shape)
            corrections.append(moved | {"name": record["name"], "dataSource": record["dataSource"]})
    if corrections:
        await store_read_correction(sessions, "steps", *corrections)


async def compare(sessions, monkeypatch, today, zone, *, fallback=False):
    full = reader._full_step_bounds
    fallbacks = []

    async def counted(*args):
        fallbacks.append(args)
        return await full(*args)

    monkeypatch.setattr(reader, "_full_step_bounds", counted)
    async with sessions() as session:
        before = await snapshot(session)
        expected = await reference_bounds(session, today, zone)
        for first_days in (1, 4, 64):
            monkeypatch.setattr(reader, "_BOUNDS_FIRST_DAYS", first_days)
            found = await reader.archive_bounds(session, "steps", today, zone)
            assert found == expected, (first_days, zone.key, today)
        assert await snapshot(session) == before
    # Consistent archives never need the full read: the windows alone produced the result.
    assert bool(fallbacks) == fallback, fallbacks
    return expected


@pytest.mark.parametrize("seed", range(48))
async def test_windowed_bounds_equal_the_full_sweep(sessions, monkeypatch, seed):
    rng = random.Random(f"step-bounds-{seed}")
    records, shape = archive(rng)
    await store_read_correction(sessions, "steps", *records)
    await mutate(sessions, rng, records, shape)
    for _ in range(3):
        zone = ZoneInfo(rng.choice(ZONES))
        today = DAY - timedelta(days=rng.choice((0, 0, 1, 40, 200)))
        await compare(sessions, monkeypatch, today, zone)


def day_record(name, day, count=100, *, hours=1, offset=12, source=BAND):
    start = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=offset)
    return steps(name, count, start, start + timedelta(hours=hours), source=source)


# (name, records, expected UTC bounds for DAY): the edges the windows rely on.
EDGES = {
    "empty": ([], ArchiveBounds(None, None)),
    "single": ([day_record("one", DAY - timedelta(days=90))], None),
    "single-across-midnight": (
        [day_record("one", DAY - timedelta(days=90), offset=22, hours=4)],
        None,
    ),
    "no-valid-day": (
        [day_record(str(i), DAY - timedelta(days=7 * i), -1) for i in range(30)],
        ArchiveBounds(None, None),
    ),
    "only-overflowing-days": (
        [
            day_record(f"{i}{part}", DAY - timedelta(days=9 * i), 1e308)
            for i in range(12)
            for part in "ab"
        ],
        ArchiveBounds(None, None),
    ),
    # A long invalid prefix and suffix: both ends widen until they reach the middle.
    "valid-middle-only": (
        [day_record(f"bad{i}", DAY - timedelta(days=i), -1) for i in range(0, 300, 3)]
        + [day_record("good", DAY - timedelta(days=150))],
        ArchiveBounds(DAY - timedelta(days=150), DAY - timedelta(days=150)),
    ),
    # An interval starting long before the newest window still reaches its days.
    "long-interval-into-tail": (
        [
            day_record("old", DAY - timedelta(days=200)),
            day_record("long", DAY - timedelta(days=30), 5000, hours=24 * 29),
            day_record("late-bad", DAY - timedelta(days=1), -1),
        ],
        None,
    ),
    # The earliest day's only other platform overflows: the measured one decides.
    "overflow-beside-measurement": (
        [
            day_record("a", DAY - timedelta(days=60), 1e308),
            day_record("b", DAY - timedelta(days=60), 1e308),
            day_record("phone", DAY - timedelta(days=60), 0, source=PHONE),
            day_record("late", DAY - timedelta(days=2)),
        ],
        ArchiveBounds(DAY - timedelta(days=60), DAY - timedelta(days=2)),
    ),
    # Read with four-day windows, the first newest window starts at DAY - 4. In
    # Pacific/Kiritimati (UTC+14) the hour before it and the hour after it both fall on
    # DAY - 4: the left-out hour overflows that day, so only DAY - 3 on is complete.
    "left-out-part-of-the-tail-edge-day": (
        [
            day_record("valid", DAY - timedelta(days=200)),
            day_record("before-edge", DAY - timedelta(days=5), 1e308, offset=22),
            day_record("after-edge", DAY - timedelta(days=4), 1e308, offset=0),
            day_record("newest", DAY, -1),
        ],
        None,
    ),
    # Starts after the read's stop are not candidates; one straddling it is clipped.
    "beyond-stop": (
        [
            day_record("future", DAY + timedelta(days=3)),
            day_record("straddle", DAY, offset=23, hours=5),
            day_record("old", DAY - timedelta(days=40)),
        ],
        ArchiveBounds(DAY - timedelta(days=40), DAY),
    ),
}


@pytest.mark.parametrize("edge", EDGES)
@pytest.mark.parametrize("zone_name", ["UTC", "Europe/Brussels", "Pacific/Kiritimati"])
async def test_window_edges(sessions, monkeypatch, edge, zone_name):
    records, expected = EDGES[edge]
    if records:
        await store_read_correction(sessions, "steps", *records)
    found = await compare(sessions, monkeypatch, DAY, ZoneInfo(zone_name))
    if expected is not None and zone_name == "UTC":
        assert found == expected


@pytest.mark.parametrize(
    "name,moved", [("r60", -300), ("r110", -1)], ids=["read-too-early", "read-too-late"]
)
async def test_indexed_bounds_out_of_payload_order_fall_back_to_the_full_read(
    sessions, monkeypatch, name, moved
):
    """A row whose indexed bounds disagree with its payload makes the windows unreliable."""
    await store_read_correction(
        sessions,
        "steps",
        *(day_record(f"r{i}", DAY - timedelta(days=i)) for i in range(0, 120, 2)),
    )
    start = T0 + timedelta(days=moved, hours=12)
    async with sessions.begin() as session:
        # Read in another row's place: its payload is out of order with its neighbours.
        await session.execute(
            update(RawHealthData)
            .where(RawHealthData.google_data_point_id == f"synthetic/steps/{name}")
            .values(source_start_time=start, source_end_time=start + timedelta(hours=1))
        )
    await compare(sessions, monkeypatch, DAY, ZoneInfo("UTC"), fallback=True)
