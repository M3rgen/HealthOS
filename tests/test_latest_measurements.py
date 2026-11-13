"""Shared latest-measurement and conflict semantics across profile consumers."""

from datetime import timedelta
from decimal import Decimal

import pytest
from derived_records import DAY, T0, manual, snapshot, store
from weight_records import weight

from app.models.manual import ManualMeasurement
from app.services.archive.manual_flags import record_flag
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_profile import dashboard_profile
from app.services.interpretation.canonical_daily import bounds, canonical_day
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources


@pytest.mark.parametrize("metric", ["weight", "height", "body_fat", "muscle"])
@pytest.mark.parametrize("tied_value", [80, 90])
async def test_latest_is_measurement_time_before_stop_with_flags_and_ties(
    sessions, metric, tied_value
):
    _, stop = bounds(DAY, "UTC")
    async with sessions.begin() as session:
        rows = [
            ManualMeasurement(
                metric=metric,
                value=Decimal(value),
                measured_at=measured,
                created_at=created,
            )
            for value, measured, created in (
                (70, T0 - timedelta(days=1), T0),
                (80, T0, T0),
                (tied_value, T0, T0 + timedelta(hours=1)),
                (60, T0 - timedelta(hours=1), stop + timedelta(days=2)),
                (100, stop - timedelta(seconds=1), stop),
                (110, stop, stop),
                (120, stop + timedelta(days=1), stop),
            )
        ]
        session.add_all(rows)
        await session.flush()
        await record_flag(session, rows[4].id, "test", True, "synthetic")
    async with sessions() as session:
        before = await snapshot(session)
        profile = await dashboard_profile(session, stop, DashboardSources())
        canonical = await canonical_day(session, DAY, "UTC")
        assert profile[metric]["value"] == (80 if tied_value == 80 else None)
        assert profile[metric]["measured_at"] == T0
        [evidence] = canonical["manual_profile"]
        assert {row["record"] for row in evidence["observations"]} == {
            str(rows[1].id),
            str(rows[2].id),
        }
        assert {row["value"] for row in evidence["observations"]} == {80, tied_value}
        assert evidence["value"] == (80 if tied_value == 80 else None)
        assert evidence["uncertainties"] == ([] if tied_value == 80 else ["conflicting_values"])
        assert profile[metric].get("uncertainties", []) == evidence["uncertainties"]
        assert await snapshot(session) == before
    # Undoing the flag restores the same latest measurement for both consumers.
    async with sessions.begin() as session:
        await record_flag(session, rows[4].id, "test", False, "synthetic")
    async with sessions() as session:
        profile = await dashboard_profile(session, stop, DashboardSources())
        canonical = await canonical_day(session, DAY, "UTC")
        assert profile[metric]["value"] == 100
        assert canonical["manual_profile"][0]["value"] == 100


async def test_no_eligible_measurement_is_absent_in_both_consumers(sessions):
    _, stop = bounds(DAY, "UTC")
    async with sessions.begin() as session:
        row = ManualMeasurement(metric="weight", value=Decimal(80), measured_at=T0)
        session.add(row)
        await session.flush()
        await record_flag(session, row.id, "test", True, "synthetic")
    async with sessions() as session:
        assert await dashboard_profile(session, stop, DashboardSources()) == {}
        assert (await canonical_day(session, DAY, "UTC"))["manual_profile"] == []


@pytest.mark.parametrize("tied_value", [70, 71])
@pytest.mark.parametrize("google_offset", [None, -1, 0])
async def test_dashboard_and_canonical_weight_agree_without_google_fallback(
    sessions, tied_value, google_offset
):
    if google_offset is not None:
        await store(sessions, "weight", weight(grams=72000, at=T0 + timedelta(hours=google_offset)))
        async with sessions() as session:
            google = await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources())
            assert google["weight"]["value"] == 72 and google["weight"]["source"] == "Google"
    await manual(sessions, 70)
    await manual(sessions, tied_value)
    async with sessions() as session:
        before = await snapshot(session)
        data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"))
        [canonical] = (await canonical_day(session, DAY, "UTC"))["manual_profile"]
        profile = data["profile"]["weight"]
        assert profile["value"] == canonical["value"] == (70 if tied_value == 70 else None)
        assert profile["source"] == "Manuel" and profile["measured_at"] == T0
        assert (
            profile.get("uncertainties", [])
            == canonical["uncertainties"]
            == ([] if tied_value == 70 else ["conflicting_values"])
        )
        assert await snapshot(session) == before
