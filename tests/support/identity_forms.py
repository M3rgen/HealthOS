"""Helpers for a device field Google added to a source's records (synthetic data only)."""

from __future__ import annotations

from datetime import timedelta

from identity_records import HEART, T0, legacy
from sqlalchemy import select

from app.models.state import RawHealthData
from app.parsers.identity import source_fingerprint
from app.services.identities.identity_backfill import identity_backfill

HOUR = timedelta(hours=1)


MINUTE = timedelta(minutes=1)


BAND = "Example Band"


FORM = "FITNESS_BAND"


def band(name: str | None = BAND, form: str | None = None, maker: str | None = None) -> dict:
    device: dict = {}
    if name is not None:
        device["displayName"] = name
    if form is not None:
        device["formFactor"] = form
    if maker is not None:
        device["manufacturer"] = maker
    source: dict = {"platform": "FITBIT", "recordingMethod": "DERIVED"}
    if device:
        source["device"] = device
    return source


def sample(value: int, at, source: dict) -> dict:
    return {
        "dataSource": source,
        "heartRate": {"sampleTime": {"physicalTime": at.isoformat()}, "beatsPerMinute": value},
    }


def fp(source: dict) -> str:
    return source_fingerprint(source)


TIMES = [T0 + index * 15 * MINUTE for index in range(4)]


VALUES = [61, 64, 58, 70]


def readings(source: dict) -> list[dict]:
    return [sample(value, at, source) for value, at in zip(VALUES, TIMES, strict=True)]


async def marked(sessions, spec=HEART) -> list[RawHealthData]:
    async with sessions() as session:
        query = select(RawHealthData).where(
            RawHealthData.data_type == spec.name, RawHealthData.source_deleted_at.is_not(None)
        )
        return list((await session.scalars(query)).all())


def values(rows) -> list[int]:
    return sorted(row.raw_json["heartRate"]["beatsPerMinute"] for row in rows)


async def old_form_archived(sessions, clock, old: dict) -> None:
    """The old form's rows, archived and reconciled (complete spans), last seen at T0 + 2h."""
    for record in readings(old):
        await legacy(sessions, HEART, record, T0 + HOUR, last=T0 + 2 * HOUR)
    clock.now = T0 + 3 * HOUR
    await identity_backfill(sessions)
