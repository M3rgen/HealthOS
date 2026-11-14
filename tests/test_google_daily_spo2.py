"""One daily oxygen field, through ingestion and all five production readers."""

from datetime import timedelta
from importlib import import_module
from itertools import combinations

import pytest
from derived_records import DAY, point, read, snapshot, store, streams, summary
from interpretation_records import call
from sqlalchemy import select, update
from support.coverage_fixtures import coverage_of
from test_google_platform_daily import project

from app.google_health.data_types import REGISTRY
from app.models.health import OxygenSaturation
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.google_daily import daily_spo2
from app.services.interpretation import canonical_daily, google_daily_readers
from app.services.synchronization import coverage_raw

KIND = "daily-oxygen-saturation"
parser = import_module("app.parsers.normalize")
# Independent expected order; distinct values make a wrong field observable.
FIELDS = ("averagePercentage", "percentage", "average")
CASES = [
    ({field: 95 + i for i, field in enumerate(fields)}, fields[0], 95)
    for size in (1, 2, 3)
    for fields in combinations(FIELDS, size)
]
CASES += [
    ({"averagePercentage": value, "percentage": 97, "average": 98}, "percentage", 97)
    for value in (None, "broken", True, {}, [])
]
CASES += [
    ({"averagePercentage": "broken", "percentage": None, "average": 98}, "average", 98),
    ({"averagePercentage": 0, "percentage": 97, "average": 98}, "averagePercentage", 0),
    ({"percentage": "-0", "average": 98}, "percentage", 0),
    ({"spo2": 99, "average": 98}, "average", 98),
]
CASES += [
    (
        {
            **dict.fromkeys(FIELDS[:i], "broken"),
            field: value,
            **{alias: 97 + j for j, alias in enumerate(FIELDS[i + 1 :])},
        },
        field,
        None,
    )
    for i, field in enumerate(FIELDS)
    for value in (-1, "-1e-999999", "-1e999999", "NaN", "sNaN", "Infinity", "-Infinity", "1e1000")
]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("fields,selected,expected", CASES)
async def test_daily_spo2_precedence_in_every_reader(
    sessions, monkeypatch, fields, selected, expected, reverse
):
    observed = {
        module: [] for module in (parser, google_daily_readers, coverage_raw, canonical_daily)
    }

    def traced(module):
        def select_field(payload):
            result = daily_spo2(payload)
            observed[module].append(result[0])
            return result

        return select_field

    for module in observed:
        monkeypatch.setattr(module, "daily_spo2", traced(module))
    record = summary(KIND, **dict(reversed(list(fields.items()))) if reverse else fields)
    stats = await store(sessions, KIND, record)
    assert stats.normalized == int(expected is not None)
    async with sessions() as session:
        before = await snapshot(session)
        row = await session.scalar(select(OxygenSaturation))
        assert (float(row.spo2) if row else None) == expected
        assert (await session.scalar(select(RawHealthData))).raw_json == record
    day, _ = await project(sessions, monkeypatch)
    [stream] = streams(await read(sessions), "spo2")
    umay = await call(sessions, "baseline", metric="spo2", end_date=str(DAY + timedelta(days=1)))
    [answer] = umay["summaries"]
    assert day["spo2"] == stream["value"] == answer["value"] == expected
    assert stream["definition"] == "daily_average"
    assert stream["state"] == ("measured" if expected is not None else "no_value")
    assert [o["field"] for o in stream["observations"]] == [selected]
    assert [o["field"] for o in answer["evidence"]] == [selected]
    metric = coverage_of(await call(sessions, "coverage", data_type=KIND), "spo2")
    assert metric["value_days"] == metric["sampling"]["sample_count"] == int(expected is not None)
    assert metric["exclusions"]["invalid_shape"] == int(expected is None)
    # Coverage exposes presence, not a scalar/field. Trace its actual selection,
    # together with the dashboard and parser, without adding response or DB fields.
    for selections in observed.values():
        assert selections and set(selections) == {selected}
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"spo2": 99}, 99),
        ({"spo2": 99, "averagePercentage": 95}, 99),
        ({"percentage": 97, "spo2": 99, "averagePercentage": 95}, 97),
    ],
)
def test_legacy_spo2_sample_alias_and_precedence_are_unchanged(fields, expected):
    record = point(
        "oxygen-saturation",
        "sample",
        {"sampleTime": {"physicalTime": "2026-09-29T12:00:00Z"}, **fields},
    )
    result = parser.normalize(REGISTRY["oxygen-saturation"], record)
    assert result.values["spo2"] == expected
    assert parser.normalize(REGISTRY[KIND], summary(KIND, spo2=99)) is None


async def test_existing_normalized_row_needs_a_changed_payload(sessions, monkeypatch):
    record = summary(KIND, averagePercentage=95, percentage=97)
    await store(sessions, KIND, record)
    # Simulate a retained row written under the old percentage-first rule.
    async with sessions.begin() as session:
        await session.execute(update(OxygenSaturation).values(spo2=97))
    stats = await store(sessions, KIND, record)
    assert stats.unchanged == 1 and stats.normalized == 0
    [stream] = streams(await read(sessions), "spo2")
    day, _ = await project(sessions, monkeypatch)
    assert stream["value"] == day["spo2"] == 95
    async with sessions() as session:
        assert await session.scalar(select(OxygenSaturation.spo2)) == 97
        assert await session.scalar(select(RawHealthDataVersion)) is None
    corrected = summary(KIND, averagePercentage=95, percentage=98)
    stats = await store(sessions, KIND, corrected)
    assert stats.updated == stats.normalized == 1
    async with sessions() as session:
        assert await session.scalar(select(OxygenSaturation.spo2)) == 95
        assert (await session.scalar(select(RawHealthDataVersion))).raw_json == record
        assert (await session.scalar(select(RawHealthData))).raw_json == corrected


@pytest.mark.parametrize("invalid", [-1, "-1e-999999", "NaN", "Infinity"])
async def test_invalid_correction_keeps_old_row_but_never_uses_it(sessions, monkeypatch, invalid):
    record = summary(KIND, averagePercentage=95, percentage=97, average=98)
    await store(sessions, KIND, record)
    stats = await store(
        sessions, KIND, summary(KIND, averagePercentage=invalid, percentage=97, average=98)
    )
    assert stats.normalized == 0 and stats.normalization_skipped == 1
    day, _ = await project(sessions, monkeypatch)
    [stream] = streams(await read(sessions), "spo2")
    [answer] = (
        await call(sessions, "baseline", metric="spo2", end_date=str(DAY + timedelta(days=1)))
    )["summaries"]
    metric = coverage_of(await call(sessions, "coverage", data_type=KIND), "spo2")
    assert day["spo2"] is stream["value"] is answer["value"] is None
    assert metric["value_days"] == 0 and metric["exclusions"]["invalid_shape"] == 1
    async with sessions() as session:
        assert await session.scalar(select(OxygenSaturation.spo2)) == 95
        assert (await session.scalar(select(RawHealthDataVersion))).raw_json == record
