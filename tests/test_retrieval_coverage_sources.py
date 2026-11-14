"""General coverage HTTP results across streamed source batches."""

from datetime import timedelta

import pytest
from coverage_source_records import KINDS, correct_and_exclude
from coverage_source_records import record as source_record
from derived_records import DAY, T0, snapshot
from support.coverage_fixtures import PATH, QUERY, fetch
from support.coverage_fixtures import api as api

from app.google_health.data_types import CIVIL_AFTER, CIVIL_BEFORE


@pytest.mark.parametrize("kind", KINDS)
async def test_general_batches_preserve_http_presence_and_receipts(
    api, sessions, monkeypatch, kind
):
    from app.services.synchronization import coverage_sources

    monkeypatch.setattr(coverage_sources, "_SOURCE_BATCH", 7)
    await fetch(
        sessions,
        start=T0 - CIVIL_BEFORE,
        end=T0 + timedelta(days=2) + CIVIL_AFTER,
        kind=kind,
        points=[source_record(kind, i) for i in range(137)],
    )
    await correct_and_exclude(sessions, kind)
    # Other types must never contribute a source or a value to this report.
    await fetch(
        sessions, kind="blood-glucose", points=[source_record("blood-glucose", i) for i in range(4)]
    )
    async with sessions() as session:
        before = await snapshot(session)
    response = await api.get(
        PATH,
        params=QUERY
        | {
            "data_type": kind,
            "start_date": str(DAY),
            "end_date": str(DAY + timedelta(days=1)),
        },
    )
    assert response.status_code == 200 and len(response.content) <= 12 * 1024
    report = response.json()["types"][0]
    assert [row["has_value"] for row in report["days"]] == [True, True]
    assert [row["retrieval"] for row in report["days"]] == ["completed_with_records"] * 2
    assert [row["returned_records"] for row in report["days"]] == [69, 68]
    assert report["exclusions"]["quarantined"] == 1 + (kind == "heart-rate")
    assert report["exclusions"]["source_deleted"] == 1
    regimes = report["source_regimes"]
    assert {row["platform"] for row in regimes} == {"FITBIT", "HEALTH_KIT"}
    assert sorted(row["record_count"] for row in regimes) == [66, 68 - (kind == "heart-rate")]
    assert all(
        row["value_days"] == 2
        and row["first_date"] == str(DAY)
        and row["last_date"] == str(DAY + timedelta(days=1))
        for row in regimes
    )
    async with sessions() as session:
        assert await snapshot(session) == before
