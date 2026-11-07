"""Normalize the Garmin raw archive into the shared normalized tables, tagged ``GARMIN``.

Reads the newest ``garmin_raw_records`` row per kind and key over a window of calendar days
and writes the shared normalized tables (``steps``, ``heart_rate``, ``sleep_sessions``, …) with
``provider = 'GARMIN'``, ``source_record_id = '<kind>:<key>'`` and the record key
``garmin:<kind>:<key>[:<part>]`` in ``google_data_point_id`` (app/core/providers.py). Nothing
Google-shaped is written: no ``raw_health_data`` row, no sighting, no identity. Google's
readers join a normalized row to its raw row by key and so never see a Garmin row; the Garmin
readers select ``provider = 'GARMIN'`` (app/services/garmin_readers.py).

Idempotent by design, not by overwrite: existing rows are read first, a row whose values are
unchanged is not written (the derived-day triggers of ``steps``/``sleep_sessions`` therefore
fire only for a new or changed row), a changed row is updated in place and a new one
inserted. Running it twice over the same archive writes nothing the second time.

What becomes what (field names as the Personal API returns them, docs/REFERENCE.md):

* ``steps`` (``totalSteps``, ``totalDistance``) -> ``steps`` and ``distance``, over the civil
  day's UTC bounds (``startTimestampGMT``/``endTimestampGMT`` of the day's ``heart-rate``
  record; UTC midnight to midnight when there is none);
* ``heart-rate`` (``heartRateValues [[ms, bpm], …]``) -> ``heart_rate``, one row per sample;
  ``restingHeartRate`` (or ``daily-summaries`` ``heartRate.restingValue``, preferred) ->
  ``resting_heart_rate`` at the civil day's UTC midnight, like Google's daily summaries;
* ``sleep`` (``sleepStartTimestampGMT``/``sleepEndTimestampGMT``, ``sleepTimeSeconds``, the
  stage seconds) -> ``sleep_sessions``; the ``sleep-summaries`` record stands in when the day
  kind is missing;
* ``hrv-summaries``/``hrv`` (``lastNightAvg``, ``status``, ``lastNight5MinHigh``) ->
  ``heart_rate_variability`` (``rmssd`` = last night's average, the rest in ``metrics``);
* ``spo2`` (``averageSpO2``) -> ``oxygen_saturation``;
* ``stress`` (``values.overallStressLevel``) and ``daily-summaries`` ``bodyBattery`` ->
  ``scalar_metrics`` as ``garmin-stress`` and ``garmin-body-battery`` (no dedicated table);
* ``activities`` (``startTimeGMT``, ``duration``, ``activityType.typeKey``, ``calories``,
  ``distance``) -> ``exercise_sessions``.

A record without the fields a row needs is skipped and counted; a value a column cannot hold
is left NULL when optional and skips the row when required (``prepare_numeric``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, DecimalException
from typing import Any, cast

from sqlalchemy import Table, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.core.logging import event
from app.core.providers import GARMIN, garmin_record_key
from app.core.timeutil import as_utc
from app.models.garmin import GarminRawRecord
from app.models.health import (
    Distance,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
)
from app.parsers import numeric
from app.parsers.text import prepare_optional_text

logger = logging.getLogger(__name__)

# The raw kinds a normalization window reads (the others, e.g. body-battery reports, stay raw).
KINDS = (
    "steps",
    "heart-rate",
    "daily-summaries",
    "sleep",
    "sleep-summaries",
    "hrv-summaries",
    "hrv",
    "spo2",
    "stress",
    "activities",
)
STRESS_TYPE = "garmin-stress"
BODY_BATTERY_TYPE = "garmin-body-battery"
# Columns compared to decide whether an existing row changed (never the bookkeeping ones).
_SKIP_COMPARE = {"id", "created_at", "updated_at", "is_quarantined", "quarantine_reason"}


@dataclass
class NormalizeStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    tables: dict[str, dict[str, int]] = field(default_factory=dict)

    def note(self, table: str, inserted: int, updated: int, unchanged: int) -> None:
        self.inserted += inserted
        self.updated += updated
        self.unchanged += unchanged
        self.tables[table] = {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "tables": self.tables,
        }


# -- reading the raw archive ---------------------------------------------------------------


async def newest_records(
    session: AsyncSession,
    kinds: Iterable[str],
    start: date,
    end: date,
    *,
    seen_since: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """kind -> key -> the newest body. Day-keyed kinds are read by key range; ``activities``
    (keyed by id) by ``first_seen_at >= seen_since`` when given, else all of them."""
    wanted = tuple(kinds)
    by_day = [kind for kind in wanted if kind != "activities"]
    rows: list[tuple[str, str, Any]] = []
    if by_day:
        rows += (
            await session.execute(
                select(GarminRawRecord.kind, GarminRawRecord.key, GarminRawRecord.body)
                .where(
                    GarminRawRecord.kind.in_(by_day),
                    GarminRawRecord.key >= start.isoformat(),
                    GarminRawRecord.key <= end.isoformat(),
                )
                .order_by(GarminRawRecord.id)
            )
        ).tuples()
    if "activities" in wanted:
        query = select(GarminRawRecord.kind, GarminRawRecord.key, GarminRawRecord.body).where(
            GarminRawRecord.kind == "activities"
        )
        if seen_since is not None:
            query = query.where(GarminRawRecord.first_seen_at >= seen_since)
        rows += (await session.execute(query.order_by(GarminRawRecord.id))).tuples()
    latest: dict[str, dict[str, Any]] = {kind: {} for kind in wanted}
    for kind, key, body in rows:
        latest[kind][key] = body  # ascending id: the newest version wins
    return latest


# -- field helpers -------------------------------------------------------------------------


def _get(record: Any, *path: str) -> Any:
    value = record
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def _number(value: Any) -> Decimal | None:
    try:
        return numeric.number(value)
    except DecimalException:
        return None


def _instant_ms(value: Any) -> datetime | None:
    """A Garmin epoch in milliseconds (``sleepStartTimestampGMT``, heart-rate samples)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _instant_text(value: Any) -> datetime | None:
    """A Garmin GMT timestamp without a zone: ``2026-11-06T00:00:00.0`` or
    ``2026-11-06 17:05:00`` (``startTimeGMT``), read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, UTC)


def _day(key: str) -> date | None:
    try:
        return date.fromisoformat(key[:10])
    except ValueError:
        return None


def day_bounds(day: date, heart_rate: Any) -> tuple[datetime, datetime]:
    """The civil day's UTC bounds from its heart-rate record, else UTC midnight to midnight."""
    start = _instant_text(_get(heart_rate, "startTimestampGMT"))
    end = _instant_text(_get(heart_rate, "endTimestampGMT"))
    if start is not None and end is not None and end > start:
        # Garmin's end is the day's last second; the interval is half open.
        return start, end + timedelta(seconds=1) if end.second == 59 else end
    return _midnight(day), _midnight(day + timedelta(days=1))


def _common(kind: str, key: str, device: str, *parts: str) -> dict[str, Any]:
    return {
        "google_data_point_id": garmin_record_key(kind, key, *parts),
        "provider": GARMIN,
        "source_record_id": f"{kind}:{key}",
        "device": device,
        "source_timezone": None,
    }


# -- rows ----------------------------------------------------------------------------------


def rows_for_day(
    day: date, records: dict[str, Any], device: str
) -> list[tuple[type[DeclarativeBase], dict[str, Any]]]:
    """The normalized rows of one calendar day from its newest records per kind."""
    key = day.isoformat()
    found: list[tuple[type[DeclarativeBase], dict[str, Any]]] = []
    steps, heart, summary = (
        records.get("steps"),
        records.get("heart-rate"),
        records.get("daily-summaries"),
    )
    start, end = day_bounds(day, heart)
    count = _number(_get(steps, "totalSteps"))
    if count is not None:
        found.append(
            (
                Steps,
                {
                    **_common("steps", key, device),
                    "start_time": start,
                    "end_time": end,
                    "count": count,
                },
            )
        )
    meters = _number(_get(steps, "totalDistance"))
    if meters is not None:
        found.append(
            (
                Distance,
                {
                    **_common("distance", key, device),
                    "start_time": start,
                    "end_time": end,
                    "meters": meters,
                },
            )
        )
    for sample in _get(heart, "heartRateValues") or ():
        if not isinstance(sample, list) or len(sample) < 2:
            continue
        moment, bpm = _instant_ms(sample[0]), _number(sample[1])
        if moment is None or bpm is None or not isinstance(sample[0], int):
            continue
        found.append(
            (
                HeartRate,
                {
                    **_common("heart-rate", key, device, str(sample[0])),
                    "timestamp": moment,
                    "bpm": bpm,
                    "motion_context": None,
                },
            )
        )
    resting = _number(_get(summary, "heartRate", "restingValue"))
    if resting is None:
        resting = _number(_get(heart, "restingHeartRate"))
    if resting is not None:
        found.append(
            (
                RestingHeartRate,
                {
                    **_common("resting-heart-rate", key, device),
                    "timestamp": _midnight(day),
                    "bpm": resting,
                    "calculation_method": None,
                },
            )
        )
    sleep = records.get("sleep") or records.get("sleep-summaries")
    sleep_start = _instant_ms(_get(sleep, "sleepStartTimestampGMT"))
    sleep_end = _instant_ms(_get(sleep, "sleepEndTimestampGMT"))
    if sleep_start is not None and sleep_end is not None and sleep_end > sleep_start:
        found.append(
            (
                SleepSession,
                {
                    **_common("sleep", key, device),
                    "start_time": sleep_start,
                    "end_time": sleep_end,
                    "duration_seconds": _number(_get(sleep, "sleepTimeSeconds")),
                    "deep_sleep_seconds": _number(_get(sleep, "deepSleepSeconds")),
                    "light_sleep_seconds": _number(_get(sleep, "lightSleepSeconds")),
                    "rem_sleep_seconds": _number(_get(sleep, "remSleepSeconds")),
                    "awake_seconds": _number(_get(sleep, "awakeSleepSeconds")),
                    "efficiency": None,
                },
            )
        )
    hrv = records.get("hrv-summaries") or _get(records.get("hrv"), "hrvSummary")
    average = _number(_get(hrv, "lastNightAvg"))
    if average is not None:
        metrics = {
            name: _get(hrv, name)
            for name in ("status", "lastNight5MinHigh", "weeklyAvg", "baseline")
            if isinstance(_get(hrv, name), str | int | float)
            and not isinstance(_get(hrv, name), bool)
        }
        found.append(
            (
                HeartRateVariability,
                {
                    **_common("hrv", key, device),
                    "timestamp": _midnight(day),
                    "rmssd": average,
                    "metrics": metrics,
                },
            )
        )
    spo2 = _number(_get(records.get("spo2"), "averageSpO2"))
    if spo2 is not None:
        found.append(
            (
                OxygenSaturation,
                {**_common("spo2", key, device), "timestamp": _midnight(day), "spo2": spo2},
            )
        )
    stress = _number(_get(records.get("stress"), "values", "overallStressLevel"))
    if stress is None:
        stress = _number(_get(summary, "stress", "avgLevel"))
    if stress is not None:
        durations = _get(records.get("stress"), "values")
        attributes = (
            {name: value for name, value in durations.items() if isinstance(value, int | float)}
            if isinstance(durations, dict)
            else {}
        )
        found.append(
            (
                ScalarMetric,
                {
                    **_common("stress", key, device),
                    "data_type": STRESS_TYPE,
                    "start_time": start,
                    "end_time": end,
                    "value": stress,
                    "unit": "level",
                    "attributes": attributes,
                },
            )
        )
    battery = _get(summary, "bodyBattery")
    if isinstance(battery, dict):
        levels = {
            name: battery.get(f"{name}Value")
            for name in ("latest", "max", "min", "charged", "drained")
            if isinstance(battery.get(f"{name}Value"), int | float)
        }
        latest = _number(levels.get("latest", levels.get("max")))
        if latest is not None:
            found.append(
                (
                    ScalarMetric,
                    {
                        **_common("body-battery", key, device),
                        "data_type": BODY_BATTERY_TYPE,
                        "start_time": start,
                        "end_time": end,
                        "value": latest,
                        "unit": "level",
                        "attributes": levels,
                    },
                )
            )
    return found


def activity_row(record: Any, device: str) -> dict[str, Any] | None:
    """An ``exercise_sessions`` row from an activity search record, or None when its id, start
    or duration is missing."""
    identifier = _get(record, "activityId")
    start = _instant_text(_get(record, "startTimeGMT"))
    duration = _number(_get(record, "duration"))
    if not isinstance(identifier, int | str) or isinstance(identifier, bool) or start is None:
        return None
    end = _instant_text(_get(record, "endTimeGMT"))
    if end is None or end <= start:
        if duration is None or duration <= 0:
            return None
        end = start + timedelta(seconds=float(duration))
    kind = _get(record, "activityType", "typeKey")
    return {
        **_common("activities", str(identifier), device),
        "start_time": start,
        "end_time": end,
        "exercise_type": kind if isinstance(kind, str) and kind else None,
        "duration_seconds": duration,
        "calories_kcal": _number(_get(record, "calories")),
        "distance_meters": _number(_get(record, "distance")),
    }


def _prepared(model: type[DeclarativeBase], values: dict[str, Any]) -> dict[str, Any] | None:
    """Fit the row to its columns (``prepare_numeric``, ``prepare_optional_text``); None when
    a required number cannot be stored."""
    row = dict(values)
    try:
        numeric.prepare_numeric(model, row)
    except DecimalException:
        return None
    prepare_optional_text(model, row)
    return row


def build_rows(
    latest: dict[str, dict[str, Any]], start: date, end: date, device: str
) -> tuple[dict[type[DeclarativeBase], list[dict[str, Any]]], int]:
    """All rows of the window per model, and how many candidate rows were skipped."""
    rows: dict[type[DeclarativeBase], list[dict[str, Any]]] = {}
    skipped = 0
    day = start
    while day <= end:
        key = day.isoformat()
        records = {kind: bodies.get(key) for kind, bodies in latest.items() if kind != "activities"}
        for model, values in rows_for_day(day, records, device):
            prepared = _prepared(model, values)
            if prepared is None:
                skipped += 1
                continue
            rows.setdefault(model, []).append(prepared)
        day += timedelta(days=1)
    for body in latest.get("activities", {}).values():
        values = activity_row(body, device)
        prepared = _prepared(ExerciseSession, values) if values is not None else None
        if prepared is None:
            skipped += 1
            continue
        activity_start = as_utc(prepared["start_time"]).date()
        # Activities keyed by id: only those that begin in the window (give or take a day for
        # the civil/UTC offset) belong to it.
        if start - timedelta(days=1) <= activity_start <= end + timedelta(days=1):
            rows.setdefault(ExerciseSession, []).append(prepared)
    return rows, skipped


# -- writing -------------------------------------------------------------------------------


def _comparable(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, Decimal):
        return value.normalize()
    return value


async def write_rows(
    session: AsyncSession, model: type[DeclarativeBase], rows: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """Insert the new rows, update the changed ones, leave the rest alone.

    Returns (inserted, updated, unchanged). Rows are matched by ``google_data_point_id``
    (and ``data_type`` for scalar metrics). Only ``provider = 'GARMIN'`` rows are written
    onto: a Google row under the same key (impossible by construction) would be left as is.
    """
    from app.services.storage import dialect_insert

    if not rows:
        return 0, 0, 0
    table = cast(Table, model.__table__)
    keys = sorted({row["google_data_point_id"] for row in rows})
    columns = [column.name for column in table.columns if column.name not in _SKIP_COMPARE]
    existing: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(keys), 1000):
        found = await session.execute(
            select(*(table.c[name] for name in ("id", *columns))).where(
                table.c.google_data_point_id.in_(keys[offset : offset + 1000])
            )
        )
        for row in found.mappings():
            existing[row["google_data_point_id"]] = dict(row)
    inserts: list[dict[str, Any]] = []
    inserted = updated = unchanged = 0
    now = datetime.now(UTC)
    for row in rows:
        current = existing.get(row["google_data_point_id"])
        if current is None:
            inserts.append(row)
            continue
        if current["provider"] != GARMIN:
            unchanged += 1
            continue
        changed = {
            name: value
            for name, value in row.items()
            if name in current and _comparable(current[name]) != _comparable(value)
        }
        if not changed:
            unchanged += 1
            continue
        await session.execute(
            update(table)
            .where(table.c.id == current["id"])
            .values(**changed, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        updated += 1
    if inserts:
        conflict = ["google_data_point_id"]
        if "data_type" in table.c:
            conflict.insert(0, "data_type")
        statement = dialect_insert(session, table).on_conflict_do_nothing(index_elements=conflict)
        for offset in range(0, len(inserts), 1000):
            await session.execute(statement, inserts[offset : offset + 1000])
        inserted = len(inserts)
    return inserted, updated, unchanged


async def normalize_window(
    session: AsyncSession,
    start: date,
    end: date,
    *,
    device: str = "Garmin",
    seen_since: datetime | None = None,
) -> NormalizeStats:
    """Normalize every Garmin record of ``start``..``end`` (calendar days, inclusive) into the
    shared normalized tables; idempotent (see the module docstring). ``seen_since`` limits
    the activities read to those first archived since then (a pull's own start)."""
    latest = await newest_records(session, KINDS, start, end, seen_since=seen_since)
    rows, skipped = build_rows(latest, start, end, device)
    stats = NormalizeStats(skipped=skipped)
    for model in sorted(rows, key=lambda item: item.__tablename__):
        model_rows = sorted(rows[model], key=lambda row: row["google_data_point_id"])
        inserted, updated, unchanged = await write_rows(session, model, model_rows)
        stats.note(model.__tablename__, inserted, updated, unchanged)
    event(
        logger,
        "GARMIN_NORMALIZED",
        start=start.isoformat(),
        end=end.isoformat(),
        inserted=stats.inserted,
        updated=stats.updated,
        unchanged=stats.unchanged,
        skipped=stats.skipped,
    )
    return stats
