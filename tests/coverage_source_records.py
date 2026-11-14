"""Synthetic source/time payloads for the general coverage presence branch."""

from datetime import timedelta

from derived_records import BAND, PHONE, T0, point, store
from sqlalchemy import select

from app.google_health.data_types import REGISTRY, RecordKind
from app.models.health import HeartRate
from app.models.state import RawHealthData as Raw

KINDS = (
    "heart-rate", "exercise", "distance", "daily-respiratory-rate",
    "respiratory-rate-sleep-summary",
)  # fmt: skip


def record(kind, index, at=None, source=None):
    at = at or T0 + timedelta(days=(index // 2) % 2, hours=2)
    source = (BAND if index % 2 == 0 else PHONE) if source is None else source
    spec = REGISTRY[kind]
    if spec.kind == RecordKind.daily:
        payload = {"date": at.date().isoformat(), "breathsPerMinute": 15}
    elif spec.kind in {RecordKind.interval, RecordKind.session}:
        payload = {
            "interval": {
                "startTime": at.isoformat(),
                "endTime": (at + timedelta(hours=1)).isoformat(),
            },
            "exerciseType": "RUNNING",
            "distanceMillimeters": 1000,
        }
    else:
        payload = {
            "sampleTime": {"physicalTime": at.isoformat()},
            "beatsPerMinute": 60,
            "breathsPerMinute": 15,
        }
    return point(kind, str(index), payload, source)


async def correct_and_exclude(sessions, kind):
    # Move one record out, move another across midnight and change its source.
    await store(
        sessions,
        kind,
        record(kind, 0, T0 - timedelta(microseconds=1)),
        record(kind, 1, T0 + timedelta(days=1), BAND),
        record(kind, 200, T0 + timedelta(days=2)),
    )
    async with sessions.begin() as session:
        for index, flag in ((8, "is_quarantined"), (9, "source_deleted_at")):
            row = await session.scalar(
                select(Raw).where(Raw.google_data_point_id == record(kind, index)["name"])
            )
            setattr(row, flag, True if index == 8 else T0)
        if kind == "heart-rate":
            row = await session.scalar(
                select(HeartRate).where(HeartRate.google_data_point_id == record(kind, 10)["name"])
            )
            row.is_quarantined = True
