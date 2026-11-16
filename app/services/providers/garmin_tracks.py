"""Shared FIT projection path for original downloads, manual imports and resumable fills.

Packages are keyed by the SHA-256 of the FIT bytes and the method version, so the same
file from the Garmin download archive and from a manual upload derives exactly once.
"""

import asyncio
import base64
import binascii
import hashlib
import logging

from sqlalchemy import ColumnElement, Exists, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from app.core.fit import FIT_EXERCISE, FIT_FILE, FIT_RECORD_PREFIX, MAX_FIT_BYTES
from app.core.logging import event
from app.db.dialect import dialect_insert
from app.models.garmin_files import GarminActivityFile
from app.models.garmin_tracks import GarminTrack, GarminTrackSession
from app.models.state import RawHealthData
from app.parsers.common import source_id

from .fit_decode import FitError
from .fit_records import FitImport, parse_fit
from .fit_tracks import METHOD_VERSION, Columns, derive_track

logger = logging.getLogger(__name__)


def completed(digest: InstrumentedAttribute[str] | ColumnElement[str] | str) -> Exists:
    return exists().where(
        GarminTrack.source_sha256 == digest, GarminTrack.method_version == METHOD_VERSION
    )


def linked(digest: InstrumentedAttribute[str] | ColumnElement[str] | str) -> Exists:
    return exists().where(GarminTrackSession.source_sha256 == digest)


def session_keys(parsed: FitImport) -> list[str]:
    """The ``source_record_id`` each FIT workout of this upload is normalized under."""
    return [
        FIT_RECORD_PREFIX + source_id(FIT_EXERCISE, record)
        for record in parsed.records.get(FIT_EXERCISE, ())
    ]


async def link_sessions(session: AsyncSession, parsed: FitImport) -> None:
    """Record which content produced each FIT workout; repeated uploads add nothing."""
    keys = sorted(set(session_keys(parsed)))
    if keys:
        await session.execute(
            dialect_insert(session, GarminTrackSession)
            .values([{"source_record_id": key, "source_sha256": parsed.sha256} for key in keys])
            .on_conflict_do_nothing(index_elements=["source_record_id", "source_sha256"])
        )


async def project_track(session: AsyncSession, data: bytes) -> int:
    """Add the current method's package for these bytes once; returns 1 when added."""
    digest = hashlib.sha256(data).hexdigest()
    if await session.scalar(select(completed(digest))):
        return 0
    values = {"source_sha256": digest, "method_version": METHOD_VERSION}
    try:
        package = await asyncio.to_thread(derive_track, data)
        values.update(vars(package), status="ready")
    except FitError:
        # The original stays authoritative; an explicit invalid package ends retries.
        empty = Columns(()).pack(0)
        values.update(samples=empty, laps=empty, events=empty, summary={}, sample_count=0)
        values.update(status="invalid")
    result = await session.execute(
        dialect_insert(session, GarminTrack)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["source_sha256", "method_version"])
        .returning(GarminTrack.source_sha256)
    )
    return int(result.scalar_one_or_none() is not None)


async def refresh_track(session: AsyncSession, data: bytes) -> None:
    """Derive inside an archive write; a projection failure never undoes the original.

    The savepoint rolls back only the package. ``--rebuild`` fills it later; the log
    names the failure type only, never content.
    """
    try:
        async with session.begin_nested():
            await project_track(session, data)
    except Exception as exc:  # noqa: BLE001 - the archive write must survive any projection bug
        event(logger, "GARMIN_TRACK_DEFERRED", error_type=type(exc).__name__)


async def refresh_import(session: AsyncSession, parsed: FitImport) -> None:
    """``refresh_track`` for a manual upload, plus its workout links, under one savepoint."""
    try:
        async with session.begin_nested():
            await project_track(session, imported_bytes(parsed.file))
            await link_sessions(session, parsed)
    except Exception as exc:  # noqa: BLE001 - the upload must survive any projection bug
        event(logger, "GARMIN_TRACK_DEFERRED", error_type=type(exc).__name__)


def imported_bytes(payload: dict) -> bytes:
    """The bytes of one manual FIT archive row, checked against its recorded SHA-256."""
    content = payload.get(FIT_FILE.json_key, {})
    encoded = content.get("body") if isinstance(content, dict) else None
    # Base64 expands by 4/3. Bound before decoding a potentially corrupted legacy row.
    if not isinstance(encoded, str) or len(encoded) > (MAX_FIT_BYTES + 2) // 3 * 4:
        raise FitError("FIT arşivi geçersiz / Invalid FIT archive")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise FitError("FIT arşivi geçersiz / Invalid FIT archive") from None
    if hashlib.sha256(data).hexdigest() != content.get("sha256"):
        raise FitError("FIT arşivi geçersiz / Invalid FIT archive")
    return data


async def _fill_downloads(sessions: async_sessionmaker) -> int:
    count, cursor = 0, 0
    while True:
        async with sessions.begin() as session:
            file_id = await session.scalar(
                select(GarminActivityFile.id)
                .where(GarminActivityFile.id > cursor, ~completed(GarminActivityFile.sha256))
                .order_by(GarminActivityFile.id)
                .limit(1)
            )
            if file_id is None:
                return count
            body = await session.scalar(
                select(GarminActivityFile.body).where(GarminActivityFile.id == file_id)
            )
            count += await project_track(session, body)
            cursor = file_id


async def _fill_imports(sessions: async_sessionmaker) -> int:
    digest = RawHealthData.raw_json[FIT_FILE.json_key]["sha256"].as_string()
    count, cursor = 0, None
    while True:
        async with sessions.begin() as session:
            query = select(RawHealthData.id, RawHealthData.raw_json).where(
                RawHealthData.data_type == FIT_FILE.name, ~completed(digest) | ~linked(digest)
            )
            if cursor is not None:
                query = query.where(RawHealthData.id > cursor)
            row = (await session.execute(query.order_by(RawHealthData.id).limit(1))).one_or_none()
            if row is None:
                return count
            cursor = row.id
            try:
                data = imported_bytes(row.raw_json)
            except FitError:
                # A damaged legacy row cannot block the fill; it is skipped on every run.
                event(logger, "GARMIN_TRACK_IMPORT_UNREADABLE")
                continue
            count += await project_track(session, data)
            try:
                parsed = await asyncio.to_thread(parse_fit, data)
            except FitError:
                continue  # Archived before parsing was stricter: a package, but no workout.
            await link_sessions(session, parsed)


async def rebuild_tracks(sessions: async_sessionmaker) -> int:
    """Commit one input at a time, keyset paging IDs only; restart skips completed methods.

    Manual uploads also get their workout links (``GarminTrackSession``). An upload
    without any workout session (heart-rate only) has nothing to link and is parsed
    again on each run, as a damaged legacy row is read again.

    No cursor or batch holds FIT bodies. A cancelled/incomplete input rolls back, and
    concurrent fills converge through the package primary key.
    """
    return await _fill_downloads(sessions) + await _fill_imports(sessions)
