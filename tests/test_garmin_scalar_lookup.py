"""Garmin lookups follow each projection's unique key, including legacy exclusions."""

from datetime import UTC, datetime
from random import Random

import pytest
from sqlalchemy import event, select

from app.models.health import HeartRate, ScalarMetric
from app.services.providers.garmin_normalize_writing import write_rows


def sample(key, *, kind="garmin-stress", value=20, model=ScalarMetric):
    common = {"google_data_point_id": key, "provider": "GARMIN"}
    now = datetime(2026, 1, 1, tzinfo=UTC)
    if model is HeartRate:
        return {**common, "timestamp": now, "bpm": value}
    return {**common, "data_type": kind, "start_time": now, "value": value, "attributes": {}}


@pytest.mark.parametrize("provider", ["GARMIN", "GOOGLE"])
async def test_same_current_id_matches_only_its_type(sessions, provider):
    battery = sample("shared:v2", kind="garmin-body-battery", value=80)
    stress = {**sample("shared:v2"), "provider": provider}
    async with sessions.begin() as session:
        session.add_all([ScalarMetric(**battery), ScalarMetric(**stress)])
    async with sessions.begin() as session:
        expected = (0, 1, 0) if provider == "GARMIN" else (0, 0, 1)
        assert await write_rows(session, ScalarMetric, [sample("shared:v2", value=30)]) == expected
    async with sessions() as session:
        values = dict(
            (await session.execute(select(ScalarMetric.data_type, ScalarMetric.value))).all()
        )
    assert values == {
        "garmin-body-battery": 80,
        "garmin-stress": 30 if provider == "GARMIN" else 20,
    }


async def test_cross_type_insert_and_legacy_exclusions(sessions):
    async with sessions.begin() as session:
        session.add(
            ScalarMetric(
                **sample("shared", kind="garmin-body-battery"),
                is_quarantined=True,
                quarantine_reason="synthetic exclusion",
            )
        )
        session.add(ScalarMetric(**sample("shared:v2", kind="garmin-body-battery", value=80)))
    rows = [sample("shared:v2"), sample("shared:v2", kind="garmin-body-battery", value=80)]
    async with sessions.begin() as session:
        # Previously the stress input overwrote the body-battery row.
        assert await write_rows(session, ScalarMetric, rows) == (1, 0, 1)
        assert await write_rows(session, ScalarMetric, rows) == (0, 0, 2)
    async with sessions() as session:
        current = (await session.scalars(select(ScalarMetric))).all()
    assert len(current) == 3
    assert sum(row.is_quarantined for row in current) == 1
    assert (
        next(row for row in current if row.google_data_point_id == "shared").data_type
        == "garmin-body-battery"
    )


async def test_legacy_exclusion_stays_with_its_scalar_type(sessions):
    async with sessions.begin() as session:
        session.add(
            ScalarMetric(
                **sample("legacy", kind="garmin-body-battery"),
                is_quarantined=True,
                quarantine_reason="synthetic exclusion",
            )
        )
    rows = [sample("legacy:v2", kind=kind) for kind in ("garmin-stress", "garmin-body-battery")]
    async with sessions.begin() as session:
        assert await write_rows(session, ScalarMetric, rows) == (2, 0, 0)
    async with sessions() as session:
        found = (
            await session.scalars(
                select(ScalarMetric).where(ScalarMetric.google_data_point_id == "legacy:v2")
            )
        ).all()
    assert {row.data_type: (row.is_quarantined, row.quarantine_reason) for row in found} == {
        "garmin-stress": (False, None),
        "garmin-body-battery": (True, "synthetic exclusion"),
    }


@pytest.mark.parametrize("model", [ScalarMetric, HeartRate])
async def test_seeded_write_results_and_counters(sessions, model):
    rng = Random(726)
    rows = [sample(f"seed-{i}:v2", value=rng.randrange(40, 90), model=model) for i in range(60)]
    async with sessions.begin() as session:
        for i, row in enumerate(rows[:45]):
            old = {**row}
            if i < 15:
                old["value" if model is ScalarMetric else "bpm"] -= 1
            if 30 <= i < 45:
                old["provider"] = "GOOGLE"
            session.add(model(**old))
    async with sessions.begin() as session:
        assert await write_rows(session, model, rows) == (15, 15, 30)
        assert await write_rows(session, model, rows) == (0, 0, 60)
    async with sessions() as session:
        found = (await session.scalars(select(model))).all()
    column = "value" if model is ScalarMetric else "bpm"
    assert {row.google_data_point_id: getattr(row, column) for row in found} == {
        row["google_data_point_id"]: row[column] for row in rows
    }
    assert sum(row.provider == "GOOGLE" for row in found) == 15


@pytest.mark.parametrize("count", [0, 1000, 1001])
async def test_single_column_lookup_query_count_is_unchanged(sessions, count):
    statements = []
    engine = sessions.kw["bind"].sync_engine

    def trace(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", trace)
    try:
        async with sessions.begin() as session:
            rows = [sample(f"unversioned-{i}", model=HeartRate) for i in range(count)]
            assert await write_rows(session, HeartRate, rows) == (count, 0, 0)
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert len(statements) == (count + 999) // 1000
    assert all("data_type" not in statement for statement in statements)
