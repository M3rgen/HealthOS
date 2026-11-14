"""Sparse candidate work and equivalence to the former per-day full scan."""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest
from derived_records import BAND, DAY, PHONE, point, snapshot, steps, store
from interpretation_records import call

from app.core.records import RecordType
from app.core.timeutil import as_utc
from app.parsers.common import payload_for
from app.services.interpretation import health_interpretation
from app.services.interpretation import interpretation_intraday as intraday


def record(kind, name, start, end, value, source=BAND, method=None):
    if kind == "steps":
        result = steps(name, value, start, end, source)
    else:
        result = point(
            kind, name, {"sampleTime": {"physicalTime": start.isoformat()}, "bpm": value}, source
        )
    if method:
        payload_for(RecordType(kind), result)["measurementMethod"] = method
    return result


@pytest.mark.parametrize("kind", ["steps", "heart-rate"])
@pytest.mark.parametrize("sources", ["shared", "distinct", "missing"])
@pytest.mark.parametrize("count", [12, 40])
async def test_sparse_days_parse_once_and_visit_only_related_candidates(
    sessions, monkeypatch, kind, sources, count
):
    records = []
    for i in range(count):
        source = (
            BAND
            if sources == "shared"
            else {"platform": "FITBIT", "device": {"displayName": f"Synthetic {i}"}}
            if sources == "distinct"
            else None
        )
        day = DAY - timedelta(days=2 * i + 1)
        left, _ = intraday.bounds(day, "UTC")
        start = left + timedelta(hours=12)
        records.append(record(kind, str(i), start, start + timedelta(seconds=60), 60, source))
    await store(sessions, kind, *records)
    async with sessions() as session:
        before = await snapshot(session)
    parse = Mock(wraps=intraday._intraday_measurement)
    monkeypatch.setattr(intraday, "_intraday_measurement", parse)
    visits, plans = Counter(), 0
    original = intraday._day_plan

    def counted_plan(kind, candidates, *args):
        nonlocal plans
        plans += 1

        def counted_candidates():
            for candidate in candidates:
                visits[candidate.raw.google_data_point_id] += 1
                yield candidate

        return original(kind, counted_candidates(), *args)

    monkeypatch.setattr(intraday, "_day_plan", counted_plan)
    args = dict(start_date=str(DAY - timedelta(days=89)), data_type=kind)
    page = await call(sessions, "intraday", limit=1, **args)
    assert page["total_summaries"] == count and page["next_cursor"]
    assert parse.call_count == plans == count
    assert visits == Counter({row["name"]: 1 for row in records})
    first = page["summaries"][0]
    assert len(first["evidence"]) == 1
    assert first["supported_seconds"] == (60 if kind == "steps" else 0)
    async with sessions() as session:
        assert await snapshot(session) == before


def full_scan_days(kind, rows, starts, stops):
    """Original candidate predicates, deliberately scanning every record per day."""
    margin = timedelta(seconds=300)
    for offset, (left, stop) in enumerate(zip(starts, stops, strict=True)):
        candidates = []
        for raw in rows:
            a, b = as_utc(raw.source_start_time), as_utc(raw.source_end_time)
            if a is None:
                continue
            value, field, codes = intraday._intraday_measurement(
                kind, payload_for(RecordType(kind), raw.raw_json)
            )
            relevant = (
                left - margin <= a < stop + margin
                if kind == "heart-rate"
                else b is not None and b > a and a < stop and b > left
            )
            if relevant:
                candidates.append(intraday._Candidate(raw, a, b, value, field, codes))
        if candidates:
            yield offset, candidates


async def test_distant_heart_neighbor_at_datetime_minimum_is_not_shifted(sessions):
    stamp = datetime.min.replace(tzinfo=UTC)
    await store(sessions, "heart-rate", record("heart-rate", "minimum", stamp, stamp, 60))
    result = await call(sessions, "intraday", start_date="0001-01-02", end_date="0001-01-02")
    assert result["summaries"] == [] and result["total_summaries"] == 0


@pytest.mark.parametrize("kind", ["steps", "heart-rate"])
@pytest.mark.parametrize("day,hours", [("2026-03-29", 23), ("2026-10-25", 25)])
async def test_index_matches_full_scan_across_dst_and_midnight(
    sessions, monkeypatch, kind, day, hours
):
    middle, timezone = date.fromisoformat(day), "Europe/Paris"
    first, last = middle - timedelta(days=1), middle + timedelta(days=1)
    left, stop = intraday.bounds(middle, timezone)
    records = []
    for source_index, source in enumerate((BAND, PHONE)):
        for method in ("A", "B"):
            prefix = f"{source_index}-{method}"
            if kind == "steps":
                # A long interval repeats evidence only on touched days, with
                # physical-second rates across both daylight-saving transitions.
                records.append(
                    record(
                        kind,
                        prefix + "-long",
                        left - timedelta(days=2),
                        stop + timedelta(days=2),
                        1000,
                        source,
                        method,
                    )
                )
                spans = [
                    (-60, 0, 60),
                    (0, 60, 0),
                    (60, 120, -2),
                    (120, 180, "NaN"),
                    (180, 240, 120),
                    (180, 240, 60),
                ]
                for i, (a, b, value) in enumerate(spans):
                    records.append(
                        record(
                            kind,
                            f"{prefix}-{i}",
                            left + timedelta(seconds=a),
                            left + timedelta(seconds=b),
                            value,
                            source,
                            method,
                        )
                    )
            else:
                samples = [
                    (-301, 60),
                    (-300, 60),
                    (-299, 60),
                    (-150, 90),
                    (0, 120),
                    (150, 120),
                    (450, 120),
                    (751, 80),
                    (800, 80),
                    (800, 90),
                    (850, 60),
                    (900, -2),
                    (950, 80),
                    (1000, "NaN"),
                    (1050, 60),
                    (1100, 0),
                    (1150, 0),
                ]
                samples += [
                    ((stop - left).total_seconds() + offset, value)
                    for offset, value in [(-150, 60), (150, 120), (299, 80), (300, 80)]
                ]
                for i, (offset, value) in enumerate(samples):
                    stamp = left + timedelta(seconds=offset)
                    records.append(
                        record(
                            kind,
                            f"{prefix}-{i}",
                            stamp,
                            stamp,
                            value,
                            source,
                            method,
                        )
                    )
    await store(sessions, kind, *records)
    async with sessions() as session:
        before = await snapshot(session)
    monkeypatch.setattr(health_interpretation, "RESPONSE_BUDGET_BYTES", 8 * 1024 * 1024)
    args = dict(start_date=str(first), end_date=str(last), timezone=timezone, data_type=kind)
    indexed = await call(sessions, "intraday", limit=200, **args)
    middle_rows = [row for row in indexed["summaries"] if row["date"] == day]
    assert len(middle_rows) == 4
    assert {row["day_seconds"] for row in middle_rows} == {hours * 3600}
    if kind == "heart-rate":
        for row in middle_rows:
            names = {e["record"].rsplit("-", 1)[1] for e in row["evidence"]}
            assert "0" not in names and "1" in names  # exactly left - 300 is included
            assert "20" not in names and "19" in names  # exactly stop + 300 is excluded
            assert "conflicting_same_time_samples" in row["reason_codes"]
            assert "unsupported_or_missing_value" in row["reason_codes"]
    with monkeypatch.context() as reference:
        reference.setattr(intraday, "_candidate_days", full_scan_days)
        assert await call(sessions, "intraday", limit=200, **args) == indexed
        page = await call(sessions, "intraday", limit=1, **args)
    rest = await call(sessions, "intraday", limit=200, cursor=page["next_cursor"], **args)
    assert page["summaries"] + rest["summaries"] == indexed["summaries"]
    assert rest["next_cursor"] is None
    async with sessions() as session:
        assert await snapshot(session) == before
