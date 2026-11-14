"""Runner-ingested coverage agrees with current values and the dashboard's sleep day."""

from datetime import timedelta
from zoneinfo import ZoneInfo

import garmin_records
import pytest
from derived_records import BAND, DAY, PHONE, T0, point, sleep, snapshot, steps
from sqlalchemy import event
from support.coverage_fixtures import PATH, fetch, query
from support.postgres_reads import read_statements
from test_sleep_corrections import api as api

from app.google_health.data_types import REGISTRY
from app.services.providers.garmin_normalize import normalize_window
from app.services.synchronization import coverage_reading
from app.services.synchronization.coverage_types import Window
from app.services.synchronization.retrieval_coverage import presence
from app.services.umay.codex_health import read_health

DATES = {"start_date": str(DAY), "end_date": str(DAY), "timezone": "UTC"}


async def outputs(sessions, api, kind):
    response = await api.get(PATH, params={**DATES, "data_type": kind})
    assert response.status_code == 200, response.text
    card = await read_health(sessions, {"action": "coverage", **DATES, "data_type": kind})
    return response.json()["types"][0], card["metrics"][0]


@pytest.mark.parametrize(
    "kind,field,good",
    [
        ("weight", "weightGrams", 70000),
        ("height", "heightMillimeters", 1700),
        ("body-fat", "percentage", 20),
    ],
)
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e1000", -1, "valid"])
@pytest.mark.parametrize("corrected", [False, True], ids=["first", "correction"])
async def test_sample_presence_and_invalid_count(
    sessions, api, kind, field, good, value, corrected
):
    def record(value):
        return point(kind, "sample", {"sampleTime": {"physicalTime": T0.isoformat()}, field: value})

    for value_to_ingest in ([good] if corrected else []) + [good if value == "valid" else value]:
        await fetch(sessions, T0, T0 + timedelta(days=1), kind, points=[record(value_to_ingest)])
    async with sessions() as session:
        before = await snapshot(session)
    panel, card = await outputs(sessions, api, kind)
    present = value == "valid"
    assert panel["days"][0]["has_value"] is present
    assert panel["days"][0]["retrieval"] == "completed_with_records"
    assert card["value_days"] == int(present)
    for result in (panel, card):
        assert result["exclusions"]["invalid_shape"] == int(not present)
    assert "invalid_shape_unknown" not in card["reason_codes"]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize(
    "grams,present",
    [([1e10], False), ([70000], True), ([70000, 1e10], True), ([1e10, 70000], True)],
    ids=["unstorable", "valid", "unstorable-correction", "stored-correction"],
)
async def test_weight_that_never_normalized_has_no_value(sessions, api, grams, present):
    # 1e10 g does not fit the normalized column, so normalization skips the record.
    for value in grams:
        payload = {"sampleTime": {"physicalTime": T0.isoformat()}, "weightGrams": value}
        await fetch(
            sessions, T0, T0 + timedelta(days=1), "weight", points=[point("weight", "w", payload)]
        )
    dates = {"end_date": str(DAY + timedelta(days=1)), "days": 2}
    dashboard = (await api.get("/api/dashboard", params=dates)).json()
    assert ("weight" in dashboard["profile"]) is present
    panel, card = await outputs(sessions, api, "weight")
    assert panel["days"][0]["has_value"] is present
    assert card["value_days"] == int(present)
    for result in (panel, card):
        assert result["exclusions"]["invalid_shape"] == int(not present)


@pytest.mark.parametrize("corrected", [False, True])
@pytest.mark.parametrize("other_platform", [False, True])
async def test_sleep_presence_uses_dashboard_day(sessions, api, corrected, other_platform):
    first = sleep("first", summary={"minutesAsleep": 60})
    second = sleep(
        "second",
        start=T0 + timedelta(hours=5),
        end=T0 + timedelta(hours=6),
        summary={"minutesAsleep": 30},
    )
    records = [first, second]
    if other_platform:
        records.append(sleep("phone", source=PHONE, summary={"minutesAsleep": 45}))
    await fetch(sessions, T0, T0 + timedelta(days=1), "sleep", points=records)
    if corrected:
        second["sleep"]["summary"]["minutesAsleep"] = "NaN"
        await fetch(sessions, T0, T0 + timedelta(days=1), "sleep", points=records)
    async with sessions() as session:
        before = await snapshot(session)
    dashboard = await api.get("/api/dashboard", params={"end_date": str(DAY), "days": 1})
    assert dashboard.status_code == 200, dashboard.text
    value = dashboard.json()["days"][0]["sleep_seconds"]
    expected = (2700 if other_platform else None) if corrected else 5400
    assert value == expected
    panel, card = await outputs(sessions, api, "sleep")
    assert panel["days"][0]["has_value"] is (value is not None)
    assert card["value_days"] == int(value is not None)
    for result in (panel, card):
        assert result["exclusions"]["invalid_shape"] == int(corrected)
    assert "invalid_shape_unknown" not in card["reason_codes"]
    async with sessions() as session:
        assert await snapshot(session) == before


@pytest.mark.parametrize("google", [None, "valid", "corrected"])
async def test_google_sleep_panel_does_not_count_garmin_sleep(sessions, api, google):
    night = {
        f"sleep{edge}TimestampGMT": int(T0.timestamp() + hours * 3600) * 1000
        for edge, hours in (("Start", -2), ("End", 6))
    }
    await garmin_records.store_page(
        sessions, "sleep", [{**garmin_records.sleep(DAY), **night}], T0 + timedelta(days=1)
    )
    async with sessions.begin() as session:
        await normalize_window(session, DAY, DAY)
    record = sleep("google", summary={"minutesAsleep": 60})
    for value in {None: [], "valid": [60], "corrected": [60, "NaN"]}[google]:
        record["sleep"]["summary"]["minutesAsleep"] = value
        await fetch(sessions, T0, T0 + timedelta(days=1), "sleep", points=[record])
    # Only the panel is per Google data type; the Umay card keeps the dashboard's day.
    panel, _ = await outputs(sessions, api, "sleep")
    assert panel["days"][0]["has_value"] is (google == "valid")
    assert panel["exclusions"]["invalid_shape"] == int(google == "corrected")


@pytest.mark.parametrize("kind", ["steps", "sleep"])
async def test_card_reports_panel_invalid_count_and_keeps_unknown_manual(sessions, api, kind):
    records = (
        [steps("valid"), steps("bad", count="NaN")]
        if kind == "steps"
        else [sleep("valid"), sleep("bad", duration="NaN")]
    )
    await fetch(sessions, T0, T0 + timedelta(days=1), kind, points=records)
    panel, card = await outputs(sessions, api, kind)
    assert panel["exclusions"]["invalid_shape"] == card["exclusions"]["invalid_shape"] == 1
    assert "invalid_shape_unknown" not in card["reason_codes"]
    card = await read_health(sessions, query(DAY, DAY, data_type="muscle"))
    muscle = card["metrics"][0]
    assert muscle["exclusions"]["invalid_shape"] is None
    assert "invalid_shape_unknown" in muscle["reason_codes"]


@pytest.mark.parametrize("kind", ["weight", "sleep"])
async def test_presence_queries_do_not_grow_with_days_or_records(sessions, kind):
    metric = next(m for m in coverage_reading.METRICS if m.data_type == kind)
    statements = []
    engine = sessions.kw["bind"].sync_engine

    def trace(conn, cursor, statement, *args):
        statements.append(statement)

    async def reads():
        counts = []
        event.listen(engine, "before_cursor_execute", trace)
        try:
            for days in (1, 31):
                statements.clear()
                window = Window(
                    DAY, DAY + timedelta(days=days - 1), DAY + timedelta(days=32), ZoneInfo("UTC")
                )
                async with sessions() as session:
                    await presence(session, REGISTRY[kind], window)
                    assert not session.identity_map
                counts.append(len(read_statements(statements)))
        finally:
            event.remove(engine, "before_cursor_execute", trace)
        assert counts[0] == counts[1]
        return counts[0]

    empty = await reads()
    records = [
        sleep(
            str(i),
            start=T0 + timedelta(days=i % 31, hours=1),
            end=T0 + timedelta(days=i % 31, hours=2),
        )
        if kind == "sleep"
        else point(
            "weight",
            str(i),
            {"sampleTime": {"physicalTime": T0.isoformat()}, "weightGrams": 70000},
            BAND,
        )
        for i in range(100)
    ]
    await fetch(sessions, T0, T0 + timedelta(days=31), metric.data_type, points=records)
    assert await reads() == empty
