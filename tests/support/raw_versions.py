"""Raw version test helpers: records, a store, the version chain and a legacy dump."""

from __future__ import annotations

import json
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.google_health.data_types import REGISTRY
from app.services.archive.raw_history import (
    record_history,
)
from app.services.archive.storage import store_records

SPEC = REGISTRY["heart-rate"]


NAME = "users/1/dataTypes/heart-rate/dataPoints/abc"


def heart_rate(bpm: int, name: str = NAME) -> dict:
    return {
        "name": name,
        "heartRate": {
            "sampleTime": {"physicalTime": "2026-08-01T10:00:00Z", "utcOffset": "0s"},
            "beatsPerMinute": bpm,
        },
    }


async def store(sessions, *records, **kwargs):
    async with sessions.begin() as session:
        return await store_records(session, SPEC, list(records), **kwargs)


async def chain(sessions, google_id: str = NAME) -> list[tuple[int, bool, int, str]]:
    """(version, is_current, bpm, source) for one record, oldest first."""
    async with sessions() as session:
        records = await record_history(session, SPEC.name, google_id=google_id)
    if not records:
        return []
    return [
        (
            item["version"],
            item["is_current"],
            item["payload"]["heartRate"]["beatsPerMinute"],
            item["source"],
        )
        for item in records[0]["versions"]
    ]


def _alembic(tmp_path, monkeypatch, name: str) -> tuple[Config, str]:
    database = (tmp_path / name).as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    get_settings.cache_clear()
    return Config("alembic.ini"), database


def _legacy_dump(tmp_path, monkeypatch, name: str, rows: list[tuple[str, dict, str, str]]) -> str:
    """A restored pre-0010 dump stand-in at 0009: (google id, payload, hash, last_seen_at)."""
    config, database = _alembic(tmp_path, monkeypatch, f"{name}.db")
    command.upgrade(config, "20261020_0009")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        for google_id, record, digest, last_seen in rows:
            connection.execute(
                text(
                    "INSERT INTO raw_health_data (id, google_data_point_id, data_type, "
                    "raw_json, payload_hash, first_seen_at, last_seen_at, is_quarantined, "
                    "created_at, updated_at) VALUES (:id, :gid, 'heart-rate', :raw, :hash, "
                    "'2026-07-01 00:00:00', :seen, 0, '2026-07-01 00:00:00', :seen)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "gid": google_id,
                    "raw": json.dumps(record),
                    "hash": digest,
                    "seen": last_seen,
                },
            )
    engine.dispose()
    get_settings.cache_clear()
    return f"sqlite+aiosqlite:///{database}"
