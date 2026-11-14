"""Shared latest-measurement and conflict semantics across profile consumers."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from derived_records import DAY, PHONE, T0, manual, snapshot, store
from profile_records import CASES, measurement
from sqlalchemy import select
from weight_records import NEGATIVE_GRAMS, weight

from app.models.health import ScalarMetric, Weight
from app.models.identity_tables import RawHealthDataSupersession
from app.models.manual import ManualMeasurement
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.archive.manual_flags import record_flag
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.dashboard_profile import dashboard_profile
from app.services.interpretation.canonical_daily import bounds, canonical_day
from app.services.readers.dashboard_queries import DashboardQuery
from app.services.readers.dashboard_sources import DashboardSources


@pytest.mark.parametrize("metric", ["weight", "height", "body_fat", "muscle"])
@pytest.mark.parametrize("tied_value", [80, 90])
@pytest.mark.parametrize("group_size", [1, 2048])
async def test_latest_is_measurement_time_before_stop_with_flags_and_ties(
    sessions, metric, tied_value, group_size
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
                (tied_value, T0, T0 - timedelta(seconds=1)),
                (60, T0 - timedelta(hours=1), stop + timedelta(days=2)),
                (100, stop - timedelta(seconds=1), stop),
                (110, stop, stop),
                (120, stop + timedelta(days=1), stop),
            )
        ]
        duplicates = [
            ManualMeasurement(
                id=UUID(int=i + 2), metric=metric, value=80, measured_at=T0, created_at=T0
            )
            for i in range(group_size - 1)
        ]
        # The possibly conflicting observation follows multiple production batches.
        rows[1].id, rows[2].id = UUID(int=1), UUID(int=group_size + 1)
        session.add_all(rows + duplicates)
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
            *(str(row.id) for row in duplicates),
        }
        assert {row["value"] for row in evidence["observations"]} == {80, tied_value}
        assert evidence["observations"] == [
            dict(record=str(row.id), value=float(row.value), measured_at=T0.isoformat())
            for row in [rows[1], *duplicates, rows[2]]
        ]
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


@pytest.mark.parametrize("kind,metric,field,raw_value,value", CASES)
@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("swap_ids", [False, True])
async def test_google_latest_group_survives_order_identity_and_refetch(
    sessions, kind, metric, field, raw_value, value, conflict, reverse, swap_ids
):
    names = ["a", "z"][:: -1 if swap_ids else 1]
    records = [
        measurement(kind, field, names[0], raw_value),
        measurement(kind, field, names[1], raw_value + int(conflict)),
    ]
    await store(
        sessions, kind, measurement(kind, field, "old", raw_value - 1, T0 - timedelta(days=1))
    )
    # A real correction leaves its previous payload in the append-only history.
    seeds = [records[0], measurement(kind, field, names[1], raw_value + 2)]
    await store(sessions, kind, *seeds[:: -1 if reverse else 1])
    await store(sessions, kind, *records[:: -1 if reverse else 1])
    for fetched in (records, records[::-1]):
        await store(sessions, kind, *fetched)
        async with sessions() as session:
            before = await snapshot(session)
            item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[
                metric
            ]
            assert item == {
                "metric": metric,
                "value": None if conflict else value,
                "measured_at": T0,
                "source": "Google",
                **({"uncertainties": ["conflicting_values"]} if conflict else {}),
            }
            assert len(list(await session.scalars(select(RawHealthData)))) == 3
            [previous] = await session.scalars(select(RawHealthDataVersion))
            assert previous.raw_json == measurement(kind, field, names[1], raw_value + 2)
            assert await snapshot(session) == before


@pytest.mark.parametrize("kind,metric,field,raw_value,value", CASES)
@pytest.mark.parametrize("conflict", [False, True])
async def test_google_latest_time_group_is_not_cut_at_a_batch_boundary(
    sessions, kind, metric, field, raw_value, value, conflict
):
    # Place the differing candidate after multiple batches in the existing query order.
    records = [measurement(kind, field, f"row-{i:03}", raw_value) for i in range(250)]
    records.append(measurement(kind, field, "row-000-tail", raw_value + int(conflict)))
    await store(sessions, kind, *records[:: 1 if kind == "weight" else -1])
    await store(
        sessions, kind, measurement(kind, field, "at-stop", raw_value + 2, T0 + timedelta(days=1))
    )
    async with sessions() as session:
        before = await snapshot(session)
        item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[metric]
        assert item["value"] == (None if conflict else value)
        assert item["measured_at"] == T0
        assert item.get("uncertainties", []) == (["conflicting_values"] if conflict else [])
        assert await snapshot(session) == before


@pytest.mark.parametrize("kind,metric,field,raw_value,value", CASES)
@pytest.mark.parametrize("reverse", [False, True])
async def test_google_platform_selection_remains_the_existing_first_candidate(
    sessions, kind, metric, field, raw_value, value, reverse
):
    records = [
        measurement(kind, field, "a", raw_value),
        measurement(kind, field, "z", raw_value * 2, source=PHONE),
    ]
    await store(sessions, kind, *records[:: -1 if reverse else 1])
    async with sessions() as session:
        # Reproduce the pre-existing ordering, without introducing a platform policy.
        order = [RawHealthData.source_start_time.desc()]
        if kind == "weight":
            order.append(RawHealthData.google_data_point_id.desc())
        first = await session.scalar(
            select(RawHealthData).where(RawHealthData.data_type == kind).order_by(*order).limit(1)
        )
        expected = value * (2 if first.google_data_point_id.endswith("/z") else 1)
        item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[metric]
        assert item == {"metric": metric, "value": expected, "measured_at": T0, "source": "Google"}
    await store(
        sessions,
        kind,
        measurement(kind, field, "m-conflict", raw_value * 3, source=first.raw_json["dataSource"]),
    )
    async with sessions() as session:
        before = await snapshot(session)
        item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[metric]
        assert item["value"] is None and item["uncertainties"] == ["conflicting_values"]
        assert item["measured_at"] == T0
        assert await snapshot(session) == before


@pytest.mark.parametrize("kind,metric,field,raw_value,value", CASES)
@pytest.mark.parametrize("manual_offset", [-1, 0, 1])
async def test_google_conflict_does_not_revive_older_values_and_newer_measurements_win(
    sessions, kind, metric, field, raw_value, value, manual_offset
):
    await store(sessions, kind, measurement(kind, field, "old", raw_value, T0 - timedelta(days=1)))
    await store(
        sessions,
        kind,
        measurement(kind, field, "a", raw_value),
        measurement(kind, field, "b", raw_value + 1),
    )
    await manual(sessions, value - 1, at=T0 + timedelta(hours=manual_offset), metric=metric)
    async with sessions() as session:
        item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[metric]
        assert item["value"] == (None if manual_offset < 0 else value - 1)
        assert item["source"] == ("Google" if manual_offset < 0 else "Manuel")
        assert item["measured_at"] == T0 + timedelta(hours=max(manual_offset, 0))
        assert item.get("uncertainties", []) == (
            ["conflicting_values"] if manual_offset < 0 else []
        )
    await store(sessions, kind, measurement(kind, field, "new", raw_value, T0 + timedelta(hours=2)))
    async with sessions() as session:
        item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[metric]
        assert item == {
            "metric": metric,
            "value": value,
            "measured_at": T0 + timedelta(hours=2),
            "source": "Google",
        }


@pytest.mark.parametrize("kind,metric,field,raw_value,value", CASES)
@pytest.mark.parametrize(
    "excluded", ["raw", "normalized", "deleted", "held", "inactive_link", "deleted_holder"]
)
async def test_google_conflict_candidates_obey_existing_eligibility(
    sessions, kind, metric, field, raw_value, value, excluded
):
    await store(
        sessions,
        kind,
        measurement(kind, field, "a", raw_value),
        measurement(kind, field, "z", raw_value + 1),
    )
    async with sessions.begin() as session:
        rows = {
            row.google_data_point_id.rsplit("/", 1)[-1]: row
            for row in await session.scalars(select(RawHealthData))
        }
        candidate, holder = rows["z"], rows["a"]
        if excluded == "raw":
            candidate.is_quarantined = True
        elif excluded == "deleted":
            candidate.source_deleted_at = T0
        elif excluded == "normalized":
            model = Weight if kind == "weight" else ScalarMetric
            normalized = await session.scalar(
                select(model).where(model.google_data_point_id == candidate.google_data_point_id)
            )
            normalized.is_quarantined = True
        else:
            session.add(
                RawHealthDataSupersession(
                    old_raw_id=candidate.id,
                    new_raw_id=holder.id,
                    data_type=kind,
                    old_google_data_point_id=candidate.google_data_point_id,
                    new_google_data_point_id=holder.google_data_point_id,
                    reason="duplicate",
                    old_fingerprint="a" * 64,
                    new_fingerprint=("b" if excluded == "inactive_link" else "a") * 64,
                    same_reading=False,
                    new_payload_hash=holder.payload_hash,
                )
            )
            if excluded == "deleted_holder":
                holder.source_deleted_at = T0
    async with sessions() as session:
        before = await snapshot(session)
        item = (await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources()))[metric]
        if excluded == "inactive_link":
            assert item["value"] is None and item["uncertainties"] == ["conflicting_values"]
        else:
            assert item["value"] == pytest.approx(
                value * (raw_value + 1) / raw_value if excluded == "deleted_holder" else value
            )
            assert not item.get("uncertainties")
        assert item["measured_at"] == T0
        assert await snapshot(session) == before


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


@pytest.mark.parametrize("kind,metric,field,raw_value,value", CASES)
@pytest.mark.parametrize("negative", [*NEGATIVE_GRAMS, "-1e999999"])
@pytest.mark.parametrize("older", [False, True])
async def test_negative_body_measurement_correction_preserves_latest_group_policy(
    sessions, kind, metric, field, raw_value, value, negative, older
):
    if older:
        await store(
            sessions, kind, measurement(kind, field, "old", raw_value, T0 - timedelta(days=1))
        )
    for current in (negative, raw_value, negative, raw_value):
        expected = value if current == raw_value or (older and kind == "weight") else None
        await store(sessions, kind, measurement(kind, field, "one", current))
        async with sessions() as session:
            before = await snapshot(session)
            profile = await dashboard_profile(session, bounds(DAY, "UTC")[1], DashboardSources())
            assert profile.get(metric, {}).get("value") == expected
            if kind != "weight" or expected is not None:
                assert profile[metric]["measured_at"] == (
                    T0 - timedelta(days=1) if kind == "weight" and current == negative else T0
                )
                assert not profile[metric].get("uncertainties")
            assert await snapshot(session) == before
