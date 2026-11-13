"""Synthetic sample data: about 90 days of one invented person, for a stranger without a source.

Every value here is made up. Nothing is read from a clock, the database or the network: the same
seed, last day, time zone and moment always give the same rows, and a day's values depend only on
the seed and that day (``random.Random`` seeded with ``<seed>:<day>:<part>``), so loading again on
a later day gives the overlapping days unchanged.

The person: an adult who runs twice a week, lifts once, rides on Saturdays and walks on Sundays
(each skipped now and then), sleeps around seven hours, slowly loses a kilogram or two. The rows
have the shape the Garmin normalizer writes (``app/services/garmin_normalize.py``: one ``steps``
row per civil day, ``<kind>:<day>`` in ``source_record_id``), so the same readers serve them,
but they carry their own provider and record key (``DEMO``, ``demo:<kind>:<day>``,
app/core/providers.py) and a device name that says what they are. Rows that would lie after
``now`` (the rest of today, tonight's sleep) are not made.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.providers import DEMO, demo_record_key
from app.db.base import Base
from app.models.health import (
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    SleepSession,
    Steps,
    Weight,
)

SEED = "healthos-sample-v1"
DAYS = 90
DEVICE = "HealthOS sample data (synthetic)"
# Minutes between two heart-rate samples.
HEART_STEP = 10
# The order rows are written and removed in; every table the sample touches.
TABLES: tuple[type[Base], ...] = (
    Steps,
    HeartRate,
    RestingHeartRate,
    HeartRateVariability,
    OxygenSaturation,
    SleepSession,
    ExerciseSession,
    Weight,
)
# weekday -> (exercise type, local start, minutes, heart rate during it, steps it adds)
PLAN: dict[int, tuple[str, time, int, tuple[int, int], int]] = {
    0: ("RUNNING", time(7, 0), 35, (138, 158), 5200),
    2: ("STRENGTH_TRAINING", time(18, 30), 45, (102, 122), 600),
    3: ("RUNNING", time(7, 10), 42, (142, 166), 6100),
    5: ("CYCLING", time(9, 30), 80, (122, 146), 0),
    6: ("WALKING", time(16, 0), 60, (92, 108), 6400),
}
# (kcal per minute, metres per minute) for each exercise type.
EFFORT = {
    "RUNNING": (11.0, 165.0),
    "STRENGTH_TRAINING": (6.5, 0.0),
    "CYCLING": (9.0, 380.0),
    "WALKING": (4.5, 85.0),
}


@dataclass(frozen=True)
class SampleRows:
    """The rows of one sample, per normalized table, ready to insert."""

    rows: dict[type[Base], list[dict[str, Any]]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {model.__tablename__: len(self.rows.get(model, [])) for model in TABLES}

    def add(self, model: type[Base], values: dict[str, Any]) -> None:
        self.rows.setdefault(model, []).append(values)


def _number(value: float, places: int = 1) -> Decimal:
    return Decimal(str(round(value, places)))


def _common(kind: str, day: date, zone: ZoneInfo, *parts: str) -> dict[str, Any]:
    return {
        "google_data_point_id": demo_record_key(kind, day.isoformat(), *parts),
        "provider": DEMO,
        "source_record_id": f"{kind}:{day.isoformat()}",
        "device": DEVICE,
        "source_timezone": zone.key,
    }


def _at(day: date, moment: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, zone).astimezone(UTC)


@dataclass(frozen=True)
class _Day:
    """One day's plan, drawn from the day's own random stream."""

    day: date
    zone: ZoneInfo
    seed: str
    resting: float
    workout: tuple[str, datetime, datetime, tuple[int, int], int] | None
    bed: datetime
    wake: datetime

    @classmethod
    def plan(cls, day: date, zone: ZoneInfo, seed: str) -> _Day:
        rng = random.Random(f"{seed}:{day.isoformat()}:plan")
        ordinal = day.toordinal()
        resting = 57 + 2.5 * math.sin(ordinal / 23) + rng.uniform(-1.5, 1.5)
        planned = PLAN.get(day.weekday())
        workout = None
        if planned and rng.random() > 0.15:
            kind, start, minutes, heart, steps = planned
            begin = _at(day, start, zone) + timedelta(minutes=rng.randint(-20, 25))
            length = timedelta(minutes=minutes + rng.randint(-6, 10))
            workout = (kind, begin, begin + length, heart, steps)
        late = 35 if day.weekday() in (5, 6) else 0
        bed = _at(day - timedelta(days=1), time(23, 5), zone) + timedelta(
            minutes=late + rng.randint(-35, 40)
        )
        wake = bed + timedelta(minutes=round(rng.gauss(432, 32)))
        return cls(day, zone, seed, resting, workout, bed, wake)

    def rng(self, part: str) -> random.Random:
        """A separate stream per part of the day, so a row left out because it would lie
        after ``now`` never changes the values of another part."""
        return random.Random(f"{self.seed}:{self.day.isoformat()}:{part}")


def _daily(plan: _Day, now: datetime, rows: SampleRows) -> None:
    """The morning values (sleep, resting heart rate, HRV, SpO2) once the night is over."""
    day, zone, rng = plan.day, plan.zone, plan.rng("daily")
    if plan.wake > now:
        return
    asleep = (plan.wake - plan.bed).total_seconds()
    awake = asleep * rng.uniform(0.04, 0.08)
    deep = asleep * rng.uniform(0.14, 0.2)
    rem = asleep * rng.uniform(0.19, 0.25)
    rows.add(
        SleepSession,
        _common("sleep", day, zone)
        | {
            "start_time": plan.bed,
            "end_time": plan.wake,
            "duration_seconds": _number(asleep, 0),
            "deep_sleep_seconds": _number(deep, 0),
            "light_sleep_seconds": _number(asleep - awake - deep - rem, 0),
            "rem_sleep_seconds": _number(rem, 0),
            "awake_seconds": _number(awake, 0),
            "efficiency": None,
        },
    )
    midnight = datetime.combine(day, time.min, UTC)
    rows.add(
        RestingHeartRate,
        _common("resting-heart-rate", day, zone)
        | {"timestamp": midnight, "bpm": _number(plan.resting, 0), "calculation_method": None},
    )
    hrv = 50 - (plan.resting - 57) * 2.2 + rng.uniform(-5, 5)
    rows.add(
        HeartRateVariability,
        _common("hrv", day, zone)
        | {"timestamp": midnight, "rmssd": _number(hrv), "metrics": {"sample": True}},
    )
    rows.add(
        OxygenSaturation,
        _common("spo2", day, zone) | {"timestamp": midnight, "spo2": _number(rng.uniform(95, 98))},
    )
    if rng.random() < 0.4 or day.weekday() == 0:
        kilograms = 73.2 + 1.2 * math.sin(day.toordinal() / 60) + rng.uniform(-0.35, 0.35)
        rows.add(
            Weight,
            _common("weight", day, zone)
            | {"timestamp": plan.wake + timedelta(minutes=12), "kilograms": _number(kilograms, 2)},
        )


def _heart(plan: _Day, rng: random.Random, moment: datetime) -> float:
    resting = plan.resting
    if plan.workout and plan.workout[1] <= moment < plan.workout[2]:
        low, high = plan.workout[3]
        return rng.uniform(low, high)
    if moment < plan.wake:
        return resting + rng.uniform(-3, 3)
    hour = moment.astimezone(plan.zone).hour
    if hour >= 23:
        return resting + rng.uniform(0, 6)
    return resting + 14 + 10 * math.sin(math.pi * (hour - 7) / 16) + rng.uniform(-6, 9)


def _samples(plan: _Day, now: datetime, rows: SampleRows) -> None:
    """Heart rate every ``HEART_STEP`` minutes of the civil day, up to ``now``."""
    start = _at(plan.day, time.min, plan.zone)
    stop = min(_at(plan.day + timedelta(days=1), time.min, plan.zone), now)
    moment, rng = start, plan.rng("heart")
    while moment < stop:
        stamp = str(int(moment.timestamp() * 1000))
        rows.add(
            HeartRate,
            _common("heart-rate", plan.day, plan.zone)
            | {
                # Keyed by the instant alone: loading again in another time zone, whose
                # civil day starts elsewhere, finds the same sample instead of a second one.
                "google_data_point_id": demo_record_key("heart-rate", stamp),
                "timestamp": moment,
                "bpm": _number(_heart(plan, rng, moment), 0),
                "motion_context": None,
            },
        )
        moment += timedelta(minutes=HEART_STEP)


def _activity(plan: _Day, now: datetime, rows: SampleRows) -> None:
    """The day's steps (so far, for today) and its workout once it has ended."""
    day, zone, rng = plan.day, plan.zone, plan.rng("activity")
    start = _at(day, time.min, zone)
    stop = _at(day + timedelta(days=1), time.min, zone)
    steps = (9300 if day.weekday() >= 5 else 7400) + rng.randint(-1900, 2200)
    if plan.workout:
        kind, begin, end, _, extra = plan.workout
        steps += extra
        if end <= now:
            minutes = (end - begin).total_seconds() / 60
            kcal, metres = EFFORT[kind]
            rows.add(
                ExerciseSession,
                _common("activity", day, zone, "1")
                | {
                    "start_time": begin,
                    "end_time": end,
                    "exercise_type": kind,
                    "duration_seconds": _number(minutes * 60, 0),
                    "calories_kcal": _number(minutes * kcal * rng.uniform(0.9, 1.1), 0),
                    "distance_meters": _number(minutes * metres, 0) if metres else None,
                },
            )
    if now <= start:
        return
    share = min(1.0, (now - start) / (stop - start))
    rows.add(
        Steps,
        _common("steps", day, zone)
        | {"start_time": start, "end_time": stop, "count": _number(steps * share, 0)},
    )


def days_until(last_day: date, days: int = DAYS) -> Iterator[date]:
    """The ``days`` calendar days ending with ``last_day``, oldest first."""
    for offset in range(days - 1, -1, -1):
        yield last_day - timedelta(days=offset)


def generate(
    last_day: date, zone: ZoneInfo, now: datetime, *, days: int = DAYS, seed: str = SEED
) -> SampleRows:
    """The sample's rows for the ``days`` days ending with ``last_day`` in ``zone``, without any
    row that would lie after ``now`` (an aware moment)."""
    rows = SampleRows()
    for day in days_until(last_day, days):
        plan = _Day.plan(day, zone, seed)
        _daily(plan, now, rows)
        _activity(plan, now, rows)
        _samples(plan, now, rows)
    return rows
