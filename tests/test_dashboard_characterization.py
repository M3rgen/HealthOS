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
from app.services.interpretation.canonical_daily import METHOD, canonical_day
from app.services.interpretation.health_windows import bounds
from app.services.readers.dashboard_queries import DashboardQuery

CASES = (
    ("empty", date(2026, 9, 29), "UTC"),
    ("ordinary", date(2026, 9, 29), "UTC"),
    ("spring", date(2026, 3, 29), "Europe/Paris"),
    ("autumn", date(2026, 10, 25), "Europe/Paris"),
    ("fractional", date(2026, 9, 29), "Asia/Kolkata"),
)
# Platform candidates and selected hourly evidence intentionally change these digests.
EXPECTED = {
    ("empty", "live"): "d8657b0b1f1f8186877cda083a6f90c44213381d8d05746197c30998a02c8819",
    ("ordinary", "live"): "aca1c31618e8ea9a67c4708216b95967dfe4dca9701b079c2acced82d9992a65",
    ("spring", "live"): "0643503e55754063a77a45b824c06fa41ddce66dbe596c54b4b123ae00c9d789",
    ("autumn", "live"): "d9575d1ad92e8b0c96152c352f9c1afd5f02fbc8a67801092f15c1b96770bb61",
    ("fractional", "live"): "073c4dd42b3e08967a88f9f489fdc2d4b17832bb32c7918bdb70bd842fdf009f",
    ("empty", "deleted"): "d8657b0b1f1f8186877cda083a6f90c44213381d8d05746197c30998a02c8819",
    ("ordinary", "deleted"): "c94f129cc5d695b4381a645f4e5ffc5274d797e5a049c0dfd0c37682afadf478",
    ("spring", "deleted"): "f1ad6e5b687b2f76f98d993d9f50ccf20687129cf7806ec8b8961bd25350d512",
    ("autumn", "deleted"): "8ef93655500570b07bca6c594ef05177e847be33f17304ba51d3d23e87941238",
    ("fractional", "deleted"): "106fdc793953e6d527501c0245a69cb1a23bd6eb30798ea3874afb88b4502f36",
    ("empty", "valid"): "d8657b0b1f1f8186877cda083a6f90c44213381d8d05746197c30998a02c8819",
    ("ordinary", "valid"): "aca1c31618e8ea9a67c4708216b95967dfe4dca9701b079c2acced82d9992a65",
    ("spring", "valid"): "0643503e55754063a77a45b824c06fa41ddce66dbe596c54b4b123ae00c9d789",
    ("autumn", "valid"): "d9575d1ad92e8b0c96152c352f9c1afd5f02fbc8a67801092f15c1b96770bb61",
    ("fractional", "valid"): "073c4dd42b3e08967a88f9f489fdc2d4b17832bb32c7918bdb70bd842fdf009f",
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
        assert all(day["method_version"] == METHOD for day in canonical)
        body = JSONResponse(jsonable_encoder({"dashboard": legacy, "canonical": canonical})).body
        # Assert today's method above; keep the remaining bytes pinned to the /6 baseline.
        assert b"source-daily/6" not in body
        body = body.replace(METHOD.encode(), b"source-daily/6")
        assert sha256(body).hexdigest() == EXPECTED[name, mode]
        assert await snapshot(session) == before
