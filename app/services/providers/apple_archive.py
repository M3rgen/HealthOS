"""Write Apple Health records into the raw archive (app/models/apple.py).

``write_records`` stores each record version once (unique ``(channel, kind, key,
body_sha256)``), returns the versions that were new and keeps ``apple_sources`` current;
``store_payload`` keeps a Health Auto Export request body once per SHA-256; the
``*_import`` helpers keep the bookkeeping row of an upload, CLI import or receiver request.
Nothing here deletes or rewrites an archived record; the caller commits.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.apple.records import AppleRecord, clip
from app.core.timeutil import as_utc
from app.models.apple import AppleImport, ApplePayload, AppleRawRecord, AppleSource

BATCH = 500
RUNNING, DONE, FAILED = "running", "done", "failed"
# Health Auto Export's descriptive request headers kept with a body (never Authorization).
KEPT_HEADERS = (
    "automation-name",
    "automation-id",
    "automation-aggregation",
    "automation-period",
    "session-id",
    "content-type",
    "user-agent",
)


def kept_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The descriptive headers of a receiver request, each cut to 255 characters."""
    lowered = {name.lower(): value for name, value in headers.items()}
    return {name: lowered[name][:255] for name in KEPT_HEADERS if name in lowered}


async def store_payload(
    session: AsyncSession,
    body: bytes,
    headers: Mapping[str, str],
    *,
    channel: str = "hae",
    now: datetime | None = None,
) -> tuple[int, bool]:
    """The id of the payload row holding ``body`` and whether this call stored it (a body
    received before is not stored again).

    "Stored" comes from the insert itself (``RETURNING``), never from a later look-up: when
    two transactions insert the same body at once, PostgreSQL makes the second wait for the
    first; if the first commits, the second inserts nothing and reports a duplicate (the
    first one's row is left as it is); if it rolls back, the second stores the body."""
    from app.db.dialect import dialect_insert

    digest = hashlib.sha256(body).hexdigest()
    found = await session.scalar(select(ApplePayload.id).where(ApplePayload.body_sha256 == digest))
    if found is not None:
        return found, False
    statement = (
        dialect_insert(session, ApplePayload.__table__)
        .on_conflict_do_nothing(index_elements=["body_sha256"])
        .returning(ApplePayload.__table__.c.id)
    )
    inserted = await session.scalar(
        statement,
        {
            "received_at": now or datetime.now(UTC),
            "channel": channel,
            "headers": kept_headers(headers),
            "body": body,
            "body_sha256": digest,
            "body_size": len(body),
        },
    )
    if inserted is not None:
        return inserted, True
    existing = await session.scalar(
        select(ApplePayload.id).where(ApplePayload.body_sha256 == digest)
    )
    if existing is None:
        raise RuntimeError("an Apple payload neither inserted nor found by its digest")
    return existing, False


async def _existing(session: AsyncSession, records: Sequence[AppleRecord]) -> set[tuple]:
    """The (channel, kind, key, body_sha256) of the given records already archived."""
    found: set[tuple] = set()
    wanted = sorted({(item.channel, item.kind, item.key) for item in records})
    for offset in range(0, len(wanted), BATCH):
        chunk = wanted[offset : offset + BATCH]
        rows = await session.execute(
            select(
                AppleRawRecord.channel,
                AppleRawRecord.kind,
                AppleRawRecord.key,
                AppleRawRecord.body_sha256,
            ).where(
                tuple_(AppleRawRecord.channel, AppleRawRecord.kind, AppleRawRecord.key).in_(chunk)
            )
        )
        found.update(tuple(row) for row in rows.tuples())
    return found


async def write_records(
    session: AsyncSession,
    records: Iterable[AppleRecord],
    *,
    payload_id: int | None = None,
    import_id: int | None = None,
    now: datetime | None = None,
) -> list[AppleRecord]:
    """Archive every record version not archived yet; returns the new ones in input order
    (a version repeated inside ``records`` counts once). ``apple_sources`` is updated from
    the new versions."""
    from app.db.dialect import dialect_insert

    now = now or datetime.now(UTC)
    unique: dict[tuple, AppleRecord] = {}
    for item in records:
        unique.setdefault((item.channel, item.kind, item.key, item.digest), item)
    if not unique:
        return []
    existing = await _existing(session, list(unique.values()))
    new = [item for identity, item in unique.items() if identity not in existing]
    if not new:
        return []
    statement = dialect_insert(session, AppleRawRecord.__table__).on_conflict_do_nothing(
        index_elements=["channel", "kind", "key", "body_sha256"]
    )
    rows = [
        {
            "channel": item.channel,
            "kind": item.kind,
            "type": item.type[:128],
            "key": item.key,
            "body": item.body,
            "body_sha256": item.digest,
            "source_name": clip(item.source_name),
            "device": clip(item.device),
            "start_time": item.start,
            "end_time": item.end,
            "payload_id": payload_id,
            "import_id": import_id,
            "first_seen_at": now,
        }
        for item in new
    ]
    for offset in range(0, len(rows), BATCH):
        await session.execute(statement, rows[offset : offset + BATCH])
    await note_sources(session, new, now)
    return new


async def note_sources(
    session: AsyncSession, records: Sequence[AppleRecord], now: datetime
) -> None:
    """Merge the records' sources into ``apple_sources`` (device, newest data, types)."""
    seen: dict[str, dict[str, Any]] = {}
    for item in records:
        name = clip(item.source_name)
        if name is None:
            continue
        entry = seen.setdefault(
            name, {"device": None, "last": None, "types": set(), "channels": set(), "count": 0}
        )
        entry["device"] = clip(item.device) or entry["device"]
        moment = item.end or item.start
        if moment is not None and (entry["last"] is None or moment > entry["last"]):
            entry["last"] = moment
        entry["types"].add(item.type[:128])
        entry["channels"].add(item.channel)
        entry["count"] += 1
    if not seen:
        return
    current = {
        row.source_name: row
        for row in (
            await session.scalars(
                select(AppleSource).where(AppleSource.source_name.in_(sorted(seen)))
            )
        ).all()
    }
    for name, entry in sorted(seen.items()):
        row = current.get(name)
        if row is None:
            session.add(
                AppleSource(
                    source_name=name,
                    device=entry["device"],
                    first_seen_at=now,
                    last_seen_at=now,
                    last_data_at=entry["last"],
                    data_types=sorted(entry["types"]),
                    channels=sorted(entry["channels"]),
                    records=entry["count"],
                )
            )
            continue
        last = row.last_data_at
        if entry["last"] is not None and (last is None or entry["last"] > as_utc(last)):
            last = entry["last"]
        await session.execute(
            update(AppleSource)
            .where(AppleSource.source_name == name)
            .values(
                device=entry["device"] or row.device,
                last_seen_at=now,
                last_data_at=last,
                data_types=sorted({*row.data_types, *entry["types"]}),
                channels=sorted({*row.channels, *entry["channels"]}),
                records=row.records + entry["count"],
            )
            .execution_options(synchronize_session=False)
        )
    await session.flush()


def record_from_row(row: AppleRawRecord) -> AppleRecord:
    """The archived version as an ``AppleRecord`` (for normalizing the archive again)."""
    return AppleRecord(
        channel=row.channel,
        kind=row.kind,
        type=row.type,
        key=row.key,
        body=row.body if isinstance(row.body, dict) else {"value": row.body},
        start=as_utc(row.start_time) if row.start_time is not None else None,
        end=as_utc(row.end_time) if row.end_time is not None else None,
        source_name=row.source_name,
        device=row.device,
        digest=row.body_sha256,
    )


# -- import bookkeeping ---------------------------------------------------------------------


async def start_import(
    session: AsyncSession,
    *,
    channel: str,
    origin: str,
    filename: str | None = None,
    now: datetime | None = None,
) -> int:
    """A new ``running`` import row; returns its id (flushed, not committed)."""
    item = AppleImport(
        channel=channel,
        origin=origin,
        status=RUNNING,
        filename=clip(filename),
        started_at=now or datetime.now(UTC),
        bytes_read=0,
        records_read=0,
        records_new=0,
        rows_written=0,
        summary={},
    )
    session.add(item)
    await session.flush()
    return item.id


async def update_import(session: AsyncSession, import_id: int, **values: Any) -> None:
    """Set progress fields of an import row (``bytes_read``, ``records_read``, ...)."""
    await session.execute(
        update(AppleImport)
        .where(AppleImport.id == import_id)
        .values(**values, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


async def finish_import(
    session: AsyncSession,
    import_id: int,
    *,
    error: str | None = None,
    now: datetime | None = None,
    **values: Any,
) -> None:
    """Mark an import ``done`` (or ``failed`` with ``error``, cut to 2000 characters)."""
    await update_import(
        session,
        import_id,
        status=FAILED if error else DONE,
        error=error[:2000] if error else None,
        finished_at=now or datetime.now(UTC),
        **values,
    )


def import_as_dict(item: AppleImport) -> dict[str, Any]:
    """The status shape of an import row (no record content)."""

    def stamp(value: datetime | None) -> str | None:
        return as_utc(value).isoformat() if value is not None else None

    return {
        "id": item.id,
        "channel": item.channel,
        "origin": item.origin,
        "status": item.status,
        "filename": item.filename,
        "started_at": stamp(item.started_at),
        "finished_at": stamp(item.finished_at),
        "bytes_read": item.bytes_read,
        "records_read": item.records_read,
        "records_new": item.records_new,
        "rows_written": item.rows_written,
        "export_date": stamp(item.export_date),
        "error": item.error,
        "summary": item.summary or {},
    }
