"""Read current and earlier raw record payloads and their append-only events.

Source reading, verification, timeline merging and transactional writes live in their
own sibling modules; callers import those implementations directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.models.raw_versions import HISTORY_VIEW, RawHealthDataEvent, RawHealthDataVersion
from app.models.state import RawJSON

# The view from migration 20261020_0011, described for SQLAlchemy (not part of Base, so
# Alembic never tries to create it as a table).
history_view = Table(
    HISTORY_VIEW,
    MetaData(),
    Column("data_type", String(128)),
    Column("google_data_point_id", String(512)),
    Column("raw_id"),
    Column("version", Integer),
    Column("is_current", Boolean),
    Column("raw_json", RawJSON),
    Column("payload_hash", String(64)),
    Column("source", String(32)),
    Column("valid_from", DateTime(timezone=True)),
    Column("valid_to", DateTime(timezone=True)),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("source_start_time", DateTime(timezone=True)),
    Column("source_end_time", DateTime(timezone=True)),
    Column("source_deleted_at", DateTime(timezone=True)),
    Column("response_id", Integer),
)


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


async def record_history(
    session: AsyncSession,
    data_type: str,
    *,
    google_id: str | None = None,
    since: datetime | None = None,
    limit: int = 20,
    include_payload: bool = True,
) -> list[dict[str, Any]]:
    """Every version of the selected records, oldest first.

    With ``google_id``: that record (also when it has only one version). Otherwise the
    ``limit`` most recently changed records of ``data_type`` that have earlier versions,
    optionally only those replaced at or after ``since``.
    """
    if google_id is not None:
        keys = [google_id]
    else:
        keys = await _recently_changed_keys(session, data_type, since, limit)
    if not keys:
        return []
    rows = await session.execute(
        select(history_view)
        .where(
            history_view.c.data_type == data_type,
            history_view.c.google_data_point_id.in_(keys),
        )
        .order_by(history_view.c.google_data_point_id, history_view.c.version)
    )
    records: dict[str, dict[str, Any]] = {}
    for row in rows.mappings():
        record = records.setdefault(
            row["google_data_point_id"],
            {
                "data_type": row["data_type"],
                "google_data_point_id": row["google_data_point_id"],
                "versions": [],
            },
        )
        version = {
            "version": row["version"],
            "is_current": bool(row["is_current"]),
            "valid_from": _iso(row["valid_from"]),
            "valid_to": _iso(row["valid_to"]),
            "last_seen_at": _iso(row["last_seen_at"]),
            "source": row["source"],
            "payload_hash": row["payload_hash"],
            "response_id": row["response_id"],
        }
        if row["source_deleted_at"] is not None:
            version["source_deleted_at"] = _iso(row["source_deleted_at"])
        if include_payload:
            version["payload"] = row["raw_json"]
        record["versions"].append(version)
    events = await session.execute(
        select(
            RawHealthDataEvent.google_data_point_id,
            RawHealthDataEvent.event,
            RawHealthDataEvent.occurred_at,
            RawHealthDataEvent.previous_deleted_at,
        )
        .where(
            RawHealthDataEvent.data_type == data_type,
            RawHealthDataEvent.google_data_point_id.in_(keys),
        )
        .order_by(RawHealthDataEvent.id)
    )
    for google_id, kind, occurred, previous in events.tuples():
        if google_id in records:
            item = {"event": kind, "at": _iso(occurred)}
            if previous is not None:
                item["previous_deleted_at"] = _iso(previous)
            records[google_id].setdefault("events", []).append(item)
    return [records[key] for key in keys if key in records]


async def _recently_changed_keys(
    session: AsyncSession, data_type: str, since: datetime | None, limit: int
) -> list[str]:
    versions = RawHealthDataVersion
    query = select(versions.google_data_point_id).where(versions.data_type == data_type)
    if since is not None:
        query = query.where(versions.superseded_at >= since)
    last_change = func.max(versions.superseded_at)
    query = (
        query.group_by(versions.google_data_point_id)
        .order_by(last_change.desc().nulls_last(), versions.google_data_point_id)
        .limit(limit)
    )
    return list((await session.scalars(query)).all())
