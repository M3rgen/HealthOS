"""The supersession holder found by its key counts exactly what the id lookup counted.

``key_held_elsewhere`` used to reach a link's holder through ``raw_health_data.id``; it now
uses the holder's ``(data_type, google_data_point_id)``, which the link records and which
neither side can change (append-only links, immutable raw identity). The frozen id lookup
below is the reference: on randomized synthetic archives (quarantines on either side, source
deletions, chains, links in and out of force, holders that are deleted, quarantined or
without a normalized row) the predicate and the dashboard's responses (week, month, every
history page) are the same with either. Synthetic data only; nothing here is a measurement.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from derived_records import point, sleep, steps, summary
from sqlalchemy import select
from sqlalchemy.orm import aliased
from support.clock import freeze_app_clock

from app.api.dashboard import dashboard, metric_history
from app.google_health.data_types import supported_data_types
from app.models import eligibility, identity_queries
from app.models.eligibility import normalized_counted, raw_counted
from app.models.health import SleepSession, Steps
from app.models.identity_queries import in_force
from app.models.identity_tables import RawHealthDataSupersession
from app.models.state import RawHealthData
from app.services.dashboard_views import history_bounds
from app.services.interpretation import google_daily_readers
from app.services.readers import dashboard_sources
from app.services.shared.query_ranges import CivilDateRange
from app.services.synchronization import coverage_raw, coverage_reading
from app.services.synchronization.retrieval_coverage import coverage_report

T0 = datetime(2026, 3, 2, 6, tzinfo=UTC)
TODAY = date(2026, 3, 30)
KINDS = {"steps": Steps, "sleep": SleepSession}
RAW_ONLY = ("daily-resting-heart-rate", "active-energy-burned")


def id_held_elsewhere(data_type, google_data_point_id):
    """The lookup as it was before the holder was found by key (frozen reference)."""
    holder = aliased(RawHealthData)
    link = aliased(RawHealthDataSupersession)
    return (
        select(link.old_raw_id)
        .join(holder, holder.id == link.new_raw_id)
        .where(
            link.old_google_data_point_id == google_data_point_id,
            link.data_type == data_type,
            holder.source_deleted_at.is_(None),
            holder.is_quarantined.is_(False),
            in_force(link),
        )
        .exists()
    )


def payload(kind, name, rng, at):
    length = timedelta(minutes=rng.choice((1, 15, 60, 300)))
    if kind == "steps":
        return steps(name, rng.choice((0, 7, 120, 1500)), start=at, end=at + length)
    return sleep(name, start=at, end=at + length)


async def seed(sessions, rng: random.Random) -> None:
    raws: dict[str, list[RawHealthData]] = {kind: [] for kind in KINDS}
    async with sessions.begin() as session:
        for kind, model in KINDS.items():
            for index in range(rng.randint(12, 40)):
                name = f"{kind}-{index}"
                at = T0 + timedelta(days=rng.randint(0, 27), minutes=rng.randint(0, 1400))
                record = payload(kind, name, rng, at)
                interval = record[kind]["interval"]
                left, right = (
                    datetime.fromisoformat(interval[key]) for key in ("startTime", "endTime")
                )
                raw = RawHealthData(
                    id=uuid.uuid4(),
                    data_type=kind,
                    google_data_point_id=name,
                    raw_json=record,
                    payload_hash=f"h-{name}",
                    source_start_time=left,
                    source_end_time=right,
                    first_seen_at=at,
                    last_seen_at=at,
                    is_quarantined=rng.random() < 0.1,
                    source_deleted_at=at + timedelta(days=1) if rng.random() < 0.1 else None,
                )
                session.add(raw)
                raws[kind].append(raw)
                if rng.random() < 0.9:  # some raw rows never normalized
                    fields = {"count": 1} if kind == "steps" else {}
                    session.add(
                        model(
                            google_data_point_id=name,
                            start_time=left,
                            end_time=right,
                            is_quarantined=rng.random() < 0.1,
                            **fields,
                        )
                    )
        for kind in RAW_ONLY:  # coverage_raw and the raw branch of metric_exclusions
            raws[kind] = []
            for index in range(rng.randint(12, 40)):
                name = f"{kind}-{index}"
                day = T0.date() + timedelta(days=rng.randint(0, 27))
                at = datetime.combine(day, datetime.min.time(), UTC)
                record = (
                    summary(kind, name, day, beatsPerMinute=rng.choice((0, 52, 61, -3)))
                    if kind == "daily-resting-heart-rate"
                    else point(kind, name, {"energy": {"kcal": 1}})
                )
                raw = RawHealthData(
                    id=uuid.uuid4(),
                    data_type=kind,
                    google_data_point_id=name,
                    raw_json=record,
                    payload_hash=f"h-{name}",
                    source_start_time=at
                    + timedelta(
                        minutes=rng.randint(0, 1400) * (kind != "daily-resting-heart-rate")
                    ),
                    first_seen_at=at,
                    last_seen_at=at,
                    is_quarantined=rng.random() < 0.1,
                    source_deleted_at=at + timedelta(days=1) if rng.random() < 0.1 else None,
                )
                session.add(raw)
                raws[kind].append(raw)
        await session.flush()
        for kind, rows in raws.items():
            pairs = {
                (old.id, new.id): (old, new) for old, new in (rng.sample(rows, 2) for _ in rows)
            }
            for old, new in pairs.values():
                fingerprints = ("fp", "fp") if rng.random() < 0.7 else ("fa", "fb")
                session.add(
                    RawHealthDataSupersession(
                        old_raw_id=old.id,
                        new_raw_id=new.id,
                        data_type=kind,
                        old_google_data_point_id=old.google_data_point_id,
                        new_google_data_point_id=new.google_data_point_id,
                        reason="duplicate",
                        old_fingerprint=fingerprints[0],
                        new_fingerprint=fingerprints[1],
                        same_reading=True,
                        new_payload_hash=new.payload_hash,
                    )
                )


def id_counted_of_type(raw, data_type):
    """``raw_counted_of_type`` as it was: every type probed by row id (frozen reference)."""
    return raw_counted(raw, (data_type,))


def use(monkeypatch, lookup, counted) -> None:
    for module in (identity_queries, eligibility, dashboard_sources):
        monkeypatch.setattr(module, "key_held_elsewhere", lookup)
    for module in (google_daily_readers, coverage_reading, coverage_raw):
        monkeypatch.setattr(module, "raw_counted_of_type", counted)
    monkeypatch.setattr(history_bounds, "_CACHE", history_bounds.BoundsCache())


async def observe(sessions) -> dict:
    found: dict = {}
    async with sessions() as session:
        for kind, model in KINDS.items():
            found[kind] = set(
                await session.scalars(
                    select(model.google_data_point_id).where(normalized_counted(model))
                )
            )
            found[f"{kind}-held"] = set(
                await session.scalars(
                    select(model.google_data_point_id).where(
                        eligibility.key_held_elsewhere(kind, model.google_data_point_id)
                    )
                )
            )
        for days, end in ((7, TODAY - timedelta(days=12)), (31, TODAY)):
            found[f"dashboard-{days}"] = await dashboard(days, end, "Europe/Brussels", session)
        specs = supported_data_types("")
        for kind in (*KINDS, *RAW_ONLY):
            found[f"coverage-{kind}"] = await coverage_report(
                session,
                specs,
                kind,
                dates=CivilDateRange(TODAY - timedelta(days=27), TODAY, "Europe/Brussels"),
            )
        for metric in ("steps", "sleep"):
            page, total = 1, 1
            while page <= total:
                history = await metric_history(metric, page, "Europe/Brussels", session)
                found[f"{metric}-history-{page}"] = history
                total = history["total_pages"]
                page += 1
    return found


@pytest.mark.parametrize("seed_value", range(12))
async def test_holder_by_key_counts_what_the_id_lookup_counted(sessions, monkeypatch, seed_value):
    rng = random.Random(seed_value)
    await seed(sessions, rng)
    freeze_app_clock(monkeypatch, datetime.combine(TODAY, datetime.min.time(), UTC))
    by_key = await observe(sessions)
    use(monkeypatch, id_held_elsewhere, id_counted_of_type)
    by_id = await observe(sessions)
    assert by_key == by_id
    # Not trivial: links hide rows, others count, and the pages show values.
    assert by_key["steps"] and by_key["steps-held"]
    assert any(day["value"] for day in by_key["steps-history-1"]["days"])
    for kind in RAW_ONLY:  # the raw coverage readers saw rows on both sides of the rule
        (report,) = by_key[f"coverage-{kind}"]["types"]
        assert report["exclusions"]["superseded"] and any(d["has_value"] for d in report["days"])
