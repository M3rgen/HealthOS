"""Frozen JSON byte digests captured before the dashboard/window refactor.

Only synthetic archive data. Hash the actual JSON encoding without sorting keys so
field order, numeric representation, assumptions and source metadata stay covered.
"""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from derived_records import PHONE, point, sleep, snapshot, steps, store, summary
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from starlette.responses import JSONResponse

from app.models.manual import ManualMeasurement
from app.models.state import RawHealthData
from app.services.dashboard_views import dashboard
from app.services.identities.source_precedence import Policy
from app.services.interpretation.canonical_daily import canonical_day
from app.services.interpretation.health_windows import bounds
from app.services.readers.dashboard_queries import DashboardQuery

CASES = (
    ("empty", date(2026, 9, 29), "UTC"),
    ("ordinary", date(2026, 9, 29), "UTC"),
    ("spring", date(2026, 3, 29), "Europe/Paris"),
    ("autumn", date(2026, 10, 25), "Europe/Paris"),
    ("fractional", date(2026, 9, 29), "Asia/Kolkata"),
)
EXPECTED = {
    ("empty", "live"): "d8657b0b1f1f8186877cda083a6f90c44213381d8d05746197c30998a02c8819",
    ("ordinary", "live"): "547e941259f73a7d7d713d25c8178c51a575e356718c7e383cb35d3812265a49",
    ("spring", "live"): "51caf1a298ba1923613fee8bfd38246bd473aa1c8676ecb69d46afd7d423a350",
    ("autumn", "live"): "ce104f2032dbeec9a61a87a79eff85d6f17f06433acb415757d1ba06b8185d9a",
    ("fractional", "live"): "e4c58f7fbcbdff028f6bb79db5fc19bc19906b2f59a34db8876248a6877553e7",
    ("empty", "deleted"): "d8657b0b1f1f8186877cda083a6f90c44213381d8d05746197c30998a02c8819",
    ("ordinary", "deleted"): "7c562d9bc02aea473b139e0c3be0a554aaf638b677df4647676e96c12beef653",
    ("spring", "deleted"): "0578b92aa87a6f87e77b067c0fc6cb433dcf2c3ddcdcad66536cd6a28fd7cf52",
    ("autumn", "deleted"): "3246022bd780924188826a0157c59caf36e6c351fcaa34ffbdb8918b7eb6ad86",
    ("fractional", "deleted"): "e68d3030f4135e55b887728fb94ffc2a7f389e8da5ccc1dacfef6b2c5ddc2324",
    ("empty", "valid"): "d8657b0b1f1f8186877cda083a6f90c44213381d8d05746197c30998a02c8819",
    ("ordinary", "valid"): "547e941259f73a7d7d713d25c8178c51a575e356718c7e383cb35d3812265a49",
    ("spring", "valid"): "51caf1a298ba1923613fee8bfd38246bd473aa1c8676ecb69d46afd7d423a350",
    ("autumn", "valid"): "ce104f2032dbeec9a61a87a79eff85d6f17f06433acb415757d1ba06b8185d9a",
    ("fractional", "valid"): "e4c58f7fbcbdff028f6bb79db5fc19bc19906b2f59a34db8876248a6877553e7",
}


async def seed(sessions, day, zone):
    first, stop = bounds(day, zone)
    hour = timedelta(hours=1)
    await store(
        sessions,
        "steps",
        steps("cross", 101, first - hour / 2, first + hour / 2),
        steps("whole", 1234.5, first, stop),
        steps("zero", 0, first + hour, first + 2 * hour, PHONE),
        steps("deleted", 77, first + 2 * hour),
        steps("quarantined", 999, first + 3 * hour),
        steps("missing", 88, first + 4 * hour),
    )
    # Retain normalized evidence while the current raw count becomes missing.
    await store(sessions, "steps", steps("missing", None, first + 4 * hour))
    await store(
        sessions,
        "sleep",
        sleep("boundary", first - 8 * hour, first, summary={"minutesAsleep": 420}),
        sleep("overlap", first - 4 * hour, first + hour, summary={"minutesAsleep": 240}),
        sleep("upper", first + hour, first + 2 * hour),
        sleep("next", stop - 8 * hour, stop, summary={"minutesAsleep": 400}),
    )
    for kind, values in (
        ("daily-resting-heart-rate", {"beatsPerMinute": 61}),
        (
            "daily-heart-rate-variability",
            {"rmssd": 28, "averageHeartRateVariabilityMilliseconds": 30},
        ),
        ("daily-oxygen-saturation", {"averagePercentage": 97.5}),
    ):
        await store(sessions, kind, summary(kind, day=day, **values))
    for kind, value in (
        ("weight", {"weightGrams": 72500}),
        ("height", {"heightMillimeters": 1750}),
        ("body-fat", {"percentage": 21}),
        ("heart-rate", {"beatsPerMinute": 72}),
    ):
        await store(
            sessions,
            kind,
            point(kind, "one", {"sampleTime": {"physicalTime": first.isoformat()}, **value}),
        )
    async with sessions.begin() as session:
        for name, field in (("deleted", "source_deleted_at"), ("quarantined", "is_quarantined")):
            raw = await session.scalar(
                select(RawHealthData).where(
                    RawHealthData.google_data_point_id == f"synthetic/steps/{name}"
                )
            )
            setattr(raw, field, first if field == "source_deleted_at" else True)
        session.add_all(
            ManualMeasurement(
                id=UUID(int=index),
                metric=metric,
                value=value,
                measured_at=first,
                created_at=first,
                updated_at=first,
            )
            for index, (metric, value) in enumerate(
                (("weight", 70), ("height", 176), ("body_fat", 20), ("muscle", 40)), 1
            )
        )


@pytest.mark.parametrize("name,day,zone", CASES)
@pytest.mark.parametrize("mode", ["live", "deleted", "valid"])
async def test_frozen_dashboard_and_canonical_json(sessions, monkeypatch, name, day, zone, mode):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(day, datetime.min.time(), UTC).astimezone(tz)

    monkeypatch.setattr(dashboard, "datetime", Fixed)
    if name != "empty":
        await seed(sessions, day, zone)
    async with sessions() as session:
        before = await snapshot(session)
        legacy = await dashboard.dashboard_data(
            session,
            policy=Policy(),
            include_source_deleted=mode != "live",
            valid_sources_only=mode == "valid",
            query=DashboardQuery(day, 3, zone),
        )
        canonical = [
            await canonical_day(session, day - timedelta(days=offset), zone) for offset in (2, 1, 0)
        ]
        body = JSONResponse(jsonable_encoder({"dashboard": legacy, "canonical": canonical})).body
        assert sha256(body).hexdigest() == EXPECTED[name, mode]
        assert await snapshot(session) == before
