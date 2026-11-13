"""Run an Apple Health import: the export upload (UI or CLI) and a receiver request.

An export import (``import_stream``) reads the Health app's ``export.zip`` or ``export.xml``
chunk by chunk, never whole in memory or on disk: each batch of records is archived
(``apple_archive.write_records``), the new versions normalized
(``apple_normalize.normalize_records``) and committed before the next one is read, so an
interrupted import keeps what it committed and importing the same export again adds only what
is missing. The ``apple_imports`` row is committed first and updated with every batch; a live
progress snapshot is kept in this process for the status endpoint. One export import runs at a
time per process (a second one is refused with ``ImportBusy``). Unzipping and parsing run in a
worker thread, one batch of records at a time (``ExportReader.iter_feed``), never on the event
loop: a 64 KiB chunk can inflate to tens of MB of XML, and the API must keep answering.

A Health Auto Export request (``receive_hae``) is small and handled in one transaction: the
body is stored first (once per SHA-256), parsed, archived and normalized. A body that cannot
be parsed is kept as evidence with a failed import row; a body received before is answered as
a duplicate without processing it again, also when the same body arrives twice at once (the
second waits for the first and stores, archives and overwrites nothing).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import anyio
import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.apple.records import EXPORT, HAE, AppleRecord, clip
from app.core.logging import event
from app.models.apple import AppleImport
from app.services.providers import apple_archive

logger = logging.getLogger(__name__)

# Records archived and normalized per transaction.
BATCH_RECORDS = 2000
# Origins (``apple_imports.origin``).
UPLOAD, CLI, RECEIVER = "upload", "cli", "receiver"


class AppleImportError(Exception):
    """An import that could not run or finish; ``status`` is the HTTP answer for it."""

    status = 500

    def __init__(self, message: str, *, import_id: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.import_id = import_id


class ImportBusy(AppleImportError):
    status = 409


class ImportTooLarge(AppleImportError):
    status = 413


class ImportRejected(AppleImportError):
    """The file or body is not an Apple Health export / Health Auto Export payload."""

    status = 400


class ImportInterrupted(AppleImportError):
    """The upload stopped before its end (the client went away)."""

    status = 400


class ExportReader(Protocol):
    """What ``import_stream`` needs from a reader (app/apple/upload.py ``ExportUpload``)."""

    @property
    def bytes_in(self) -> int: ...

    @property
    def xml_bytes(self) -> int: ...

    @property
    def export_date(self) -> datetime | None: ...

    @property
    def counts(self) -> Mapping[str, int]: ...

    @property
    def skipped(self) -> Any: ...

    @property
    def format(self) -> str | None: ...

    @property
    def member_name(self) -> str | None: ...

    def iter_feed(self, chunk: bytes) -> Iterator[list[AppleRecord]]:
        """The records completed by ``chunk``, in small batches, produced lazily."""
        ...

    def iter_close(self) -> Iterator[list[AppleRecord]]: ...


@dataclass
class Progress:
    """The running import as the status endpoint shows it (no record content)."""

    import_id: int
    origin: str
    filename: str | None
    started_at: datetime
    bytes_read: int = 0
    xml_bytes: int = 0
    records_read: int = 0
    records_new: int = 0
    rows_written: int = 0
    format: str | None = None
    export_date: datetime | None = None
    normalize: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "origin": self.origin,
            "filename": self.filename,
            "started_at": self.started_at.isoformat(),
            "bytes_read": self.bytes_read,
            "xml_bytes": self.xml_bytes,
            "records_read": self.records_read,
            "records_new": self.records_new,
            "rows_written": self.rows_written,
            "format": self.format,
            "export_date": self.export_date.isoformat() if self.export_date else None,
        }


# One export import per process; a threading lock also holds across event loops.
_GUARD = threading.Lock()
_current: Progress | None = None


def running() -> dict[str, Any] | None:
    """The live progress of this process's export import, or None."""
    current = _current
    return current.as_dict() if current is not None else None


def merge_counts(total: dict[str, Any], part: Mapping[str, Any]) -> dict[str, Any]:
    """Add ``part`` into ``total`` (numbers summed, nested mappings merged)."""
    for name, value in part.items():
        if isinstance(value, bool):
            total[name] = value
        elif isinstance(value, int | float):
            current = total.get(name, 0)
            total[name] = (current if isinstance(current, int | float) else 0) + value
        elif isinstance(value, Mapping):
            nested = total.get(name)
            total[name] = merge_counts(nested if isinstance(nested, dict) else {}, value)
        else:
            total[name] = value
    return total


def _stats(stats: Any) -> dict[str, Any]:
    if stats is None:
        return {}
    if hasattr(stats, "as_dict"):
        return dict(stats.as_dict())
    return dict(stats) if isinstance(stats, Mapping) else {}


def _written(stats: Mapping[str, Any]) -> int:
    return int(stats.get("inserted", 0) or 0) + int(stats.get("updated", 0) or 0)


def _jsonable(value: Any) -> Any:
    """Counters as JSON (Counter, sets and tuples included)."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def _archive_batch(
    sessions: async_sessionmaker, import_id: int, batch: Sequence[AppleRecord], progress: Progress
) -> None:
    from app.services.providers.apple_normalize import normalize_records

    async with sessions() as session:
        new = await apple_archive.write_records(session, batch, import_id=import_id)
        stats = _stats(await normalize_records(session, new)) if new else {}
        progress.records_read += len(batch)
        progress.records_new += len(new)
        progress.rows_written += _written(stats)
        merge_counts(progress.normalize, stats)
        await apple_archive.update_import(
            session,
            import_id,
            bytes_read=progress.bytes_read,
            records_read=progress.records_read,
            records_new=progress.records_new,
            rows_written=progress.rows_written,
        )
        await session.commit()


async def _fail(sessions: async_sessionmaker, progress: Progress, error: str) -> None:
    try:
        async with sessions() as session:
            await apple_archive.finish_import(
                session,
                progress.import_id,
                error=error,
                bytes_read=progress.bytes_read,
                records_read=progress.records_read,
                records_new=progress.records_new,
                rows_written=progress.rows_written,
                export_date=progress.export_date,
                summary={"normalize": progress.normalize, "format": progress.format},
            )
            await session.commit()
    except Exception as exc:  # The original error is what the caller must see.
        event(logger, "APPLE_IMPORT_STATUS_WRITE_FAILED", error_type=type(exc).__name__)


def export_reader(max_uncompressed: int | None = None) -> ExportReader:
    """The streaming export.zip/export.xml reader, with its decompression limit when given."""
    from app.apple.upload import ExportUpload

    return ExportUpload() if max_uncompressed is None else ExportUpload(max_uncompressed)


def _format_errors() -> tuple[type[BaseException], ...]:
    from app.apple.export_xml import ExportFormatError

    return (ExportFormatError,)


def _too_large(exc: BaseException) -> ImportTooLarge | None:
    """The 413 answer for a zip past its decompression limits, else None."""
    from app.apple.export_zip import ExportTooLarge

    if not isinstance(exc, ExportTooLarge):
        return None
    if exc.reason == "ratio":
        return ImportTooLarge(
            f"Arşiv boyutunun {exc.limit} katından fazlasına açılıyor (zip bombası olabilir); "
            "içe aktarma durduruldu."
        )
    return ImportTooLarge(
        f"Arşiv açıldığında {exc.limit // (1024 * 1024)} MB sınırını aşıyor; "
        "içe aktarma durduruldu."
    )


def _pull(batches: Iterator[list[AppleRecord]], pending: list[AppleRecord], limit: int) -> bool:
    """Move batches from ``batches`` into ``pending`` until it holds ``limit`` records; True
    when ``batches`` is exhausted. Runs in the worker thread; resumable (the generator is not
    closed when this returns early)."""
    for batch in batches:
        pending.extend(batch)
        if len(pending) >= limit:
            return False
    return True


def export_kind(path: Path) -> str:
    """``zip`` when the file starts with a zip local file header, else ``xml``."""
    from app.apple.export_zip import LOCAL_HEADER

    with open(path, "rb") as handle:
        return "zip" if handle.read(4) == LOCAL_HEADER else "xml"


def export_chunks(path: Path, max_output: int) -> Iterator[bytes]:
    """The export.xml bytes of a file on disk (inside its zip or bare), in pieces."""
    from app.apple.export_zip import open_export_path

    return open_export_path(path, max_output=max_output)


class XmlReader:
    """An ``ExportReader`` over export.xml bytes already taken out of the zip (the CLI's
    ``open_export_path``): the parser alone, with the file's format and member name."""

    def __init__(self, format: str, member_name: str | None = None) -> None:
        from app.apple.export_xml import ExportParser

        self.parser = ExportParser()
        self.format: str | None = format
        self.member_name = member_name
        self.bytes_in = 0
        self.xml_bytes = 0

    @property
    def export_date(self) -> datetime | None:
        return self.parser.export_date

    @property
    def counts(self) -> Mapping[str, int]:
        return self.parser.counts

    @property
    def skipped(self) -> Any:
        return self.parser.skipped

    def iter_feed(self, chunk: bytes) -> Iterator[list[AppleRecord]]:
        from app.apple.export_zip import PIECE

        self.bytes_in += len(chunk)
        for offset in range(0, len(chunk), PIECE):
            piece = chunk[offset : offset + PIECE]
            self.xml_bytes += len(piece)
            yield self.parser.feed(piece)

    def iter_close(self) -> Iterator[list[AppleRecord]]:
        yield self.parser.close()

    def feed(self, chunk: bytes) -> list[AppleRecord]:
        return [record for batch in self.iter_feed(chunk) for record in batch]

    def close(self) -> list[AppleRecord]:
        return [record for batch in self.iter_close() for record in batch]


def _disconnects() -> tuple[type[BaseException], ...]:
    from starlette.requests import ClientDisconnect

    return (ClientDisconnect,)


async def _read_export(
    sessions: async_sessionmaker,
    chunks: AsyncIterator[bytes],
    source: ExportReader,
    progress: Progress,
    max_bytes: int,
) -> None:
    """Feed ``chunks`` to ``source`` and archive its records in batches of ``BATCH_RECORDS``.
    One worker thread at a time unzips and parses (``iter_feed``); the event loop only
    archives."""
    batch_size = BATCH_RECORDS
    import_id = progress.import_id
    pending: list[AppleRecord] = []
    limiter = anyio.CapacityLimiter(1)

    async def drain(batches: Callable[[], Iterator[list[AppleRecord]]]) -> None:
        produced = batches()
        while True:
            done = await anyio.to_thread.run_sync(
                _pull, produced, pending, batch_size, limiter=limiter
            )
            _note_source(progress, source)
            while len(pending) >= batch_size:
                batch = pending[:batch_size]
                del pending[:batch_size]
                await _archive_batch(sessions, import_id, batch, progress)
            if done:
                return

    async for chunk in chunks:
        if not chunk:
            continue
        if progress.bytes_read + len(chunk) > max_bytes:
            raise ImportTooLarge(
                f"Dosya {max_bytes // (1024 * 1024)} MB sınırını aşıyor.",
                import_id=import_id,
            )
        progress.bytes_read += len(chunk)
        await drain(lambda chunk=chunk: source.iter_feed(chunk))
    await drain(source.iter_close)
    if pending:
        await _archive_batch(sessions, import_id, list(pending), progress)
        pending.clear()


def _note_source(progress: Progress, source: ExportReader) -> None:
    progress.xml_bytes = source.xml_bytes
    progress.format = source.format
    progress.export_date = source.export_date or progress.export_date


async def _finish_export(
    sessions: async_sessionmaker, source: ExportReader, progress: Progress
) -> dict[str, Any]:
    """Mark the import finished with its summary; returns the finished import row."""
    import_id = progress.import_id
    summary = {
        "format": source.format,
        "member": source.member_name,
        "bytes": progress.bytes_read,
        "xml_bytes": source.xml_bytes,
        "export_date": source.export_date.isoformat() if source.export_date else None,
        "counts": _jsonable(source.counts),
        "skipped": _jsonable(source.skipped),
        "normalize": progress.normalize,
    }
    async with sessions() as session:
        await apple_archive.finish_import(
            session,
            import_id,
            bytes_read=progress.bytes_read,
            records_read=progress.records_read,
            records_new=progress.records_new,
            rows_written=progress.rows_written,
            export_date=source.export_date,
            summary=summary,
        )
        await session.commit()
        row = await session.get(AppleImport, import_id, populate_existing=True)
        if row is None:
            raise RuntimeError(f"apple import {import_id} disappeared while it ran")
        return apple_archive.import_as_dict(row)


async def import_stream(
    sessions: async_sessionmaker,
    chunks: AsyncIterator[bytes],
    *,
    origin: str,
    filename: str | None,
    max_bytes: int,
    reader: ExportReader | None = None,
) -> dict[str, Any]:
    """Import one export read from ``chunks`` (zip or xml bytes as they arrive); returns the
    finished import row (``apple_archive.import_as_dict``). Raises an ``AppleImportError``:
    ``ImportBusy`` while another import runs in this process (nothing written),
    ``ImportTooLarge`` past ``max_bytes`` or a zip past its decompression limits (the
    ``reader``'s, e.g. ``export_reader(max_uncompressed)``, and the ratio guard),
    ``ImportRejected`` for a file that is not an Apple Health export, ``ImportInterrupted``
    when the upload stops early. Batches committed before a failure stay archived; the import
    row is marked failed with the reason. Records are archived in batches of
    ``BATCH_RECORDS``."""
    global _current
    if not _GUARD.acquire(blocking=False):
        raise ImportBusy("Başka bir Apple Sağlık içe aktarması sürüyor.")
    try:
        name = clip(filename)
        async with sessions() as session:
            import_id = await apple_archive.start_import(
                session, channel=EXPORT, origin=origin, filename=name
            )
            await session.commit()
        progress = Progress(import_id, origin, name, datetime.now(UTC))
        _current = progress
        event(logger, "APPLE_IMPORT_STARTED", import_id=import_id, origin=origin)
        try:
            source = reader if reader is not None else export_reader()
            await _read_export(sessions, chunks, source, progress, max_bytes)
        except AppleImportError as exc:
            exc.import_id = import_id
            await _fail(sessions, progress, exc.message)
            event(logger, "APPLE_IMPORT_FAILED", import_id=import_id, reason=type(exc).__name__)
            raise
        except _format_errors() as exc:
            too_large = _too_large(exc)
            if too_large is not None:
                too_large.import_id = import_id
                await _fail(sessions, progress, too_large.message)
                event(logger, "APPLE_IMPORT_FAILED", import_id=import_id, reason="too_large")
                raise too_large from exc
            message = f"Apple Sağlık dışa aktarması okunamadı: {exc}"
            await _fail(sessions, progress, message)
            event(logger, "APPLE_IMPORT_FAILED", import_id=import_id, reason="format")
            raise ImportRejected(message, import_id=import_id) from exc
        except _disconnects() as exc:
            message = "Yükleme yarıda kesildi; kaydedilen kayıtlar duruyor, dosyayı yeniden yükle."
            await _fail(sessions, progress, message)
            event(logger, "APPLE_IMPORT_FAILED", import_id=import_id, reason="disconnect")
            raise ImportInterrupted(message, import_id=import_id) from exc
        except BaseException as exc:
            await _fail(sessions, progress, f"İçe aktarma durdu ({type(exc).__name__}).")
            event(logger, "APPLE_IMPORT_FAILED", import_id=import_id, reason=type(exc).__name__)
            raise
        result = await _finish_export(sessions, source, progress)
        event(
            logger,
            "APPLE_IMPORT_FINISHED",
            import_id=import_id,
            origin=origin,
            records_read=progress.records_read,
            records_new=progress.records_new,
            rows_written=progress.rows_written,
        )
        return result
    finally:
        _current = None
        _GUARD.release()


async def iterate(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    """A synchronous chunk source (a file read by the CLI) as an async one."""
    for chunk in chunks:
        yield chunk


def file_chunks(path: str, size: int = 1024 * 1024) -> Iterator[bytes]:
    """The bytes of a file in ``size`` pieces."""
    with open(path, "rb") as handle:
        while chunk := handle.read(size):
            yield chunk


# -- Health Auto Export receiver --------------------------------------------------------------


async def recent_imports(session: AsyncSession, limit: int = 10) -> list[dict[str, Any]]:
    """The newest import rows, newest first (status shape, no record content)."""
    rows = await session.scalars(
        select(AppleImport)
        .order_by(AppleImport.started_at.desc(), AppleImport.id.desc())
        .limit(limit)
    )
    return [apple_archive.import_as_dict(row) for row in rows.all()]


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        return sum(_count(item) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return len(value)
    return 0


async def _receiver_failed(sessions: async_sessionmaker, size: int, error: str) -> None:
    """A failed import row for a request rolled back by an unexpected error (no body: the
    sender retries it)."""
    try:
        async with sessions() as session:
            import_id = await apple_archive.start_import(session, channel=HAE, origin=RECEIVER)
            await apple_archive.finish_import(
                session, import_id, error=f"İşlenemedi ({error}).", bytes_read=size
            )
            await session.commit()
    except Exception as exc:
        event(logger, "APPLE_IMPORT_STATUS_WRITE_FAILED", error_type=type(exc).__name__)


class _LostRace(Exception):
    """A concurrent request committed the same unique key first; this one was rolled back."""


async def receive_hae(
    sessions: async_sessionmaker, body: bytes, headers: Mapping[str, str]
) -> dict[str, Any]:
    """Store, parse, archive and normalize one Health Auto Export request body.

    Returns ``{"status": "ok"|"duplicate", "records", "new", "rows", "skipped", "invalid"}``.
    Raises ``ImportRejected`` for a body that is not a Health Auto Export payload (the body
    and a failed import row are committed first). Any other failure rolls the request back
    (the body included), so the sender's retry processes it again.

    The same body sent twice at once: the second request waits on the first one's unique
    ``body_sha256`` and answers ``duplicate``. A request that loses any other unique-key race
    to a concurrent one (a Health source seen for the first time by both) is rolled back and
    run once more, now seeing what the other committed."""
    try:
        return await _receive_once(sessions, body, headers, last_attempt=False)
    except _LostRace:
        event(logger, "APPLE_RECEIVER_RACE_RETRY")
        return await _receive_once(sessions, body, headers, last_attempt=True)


async def _receive_once(
    sessions: async_sessionmaker, body: bytes, headers: Mapping[str, str], *, last_attempt: bool
) -> dict[str, Any]:
    from app.apple.hae import HaeFormatError, parse_hae
    from app.services.providers.apple_normalize import normalize_records

    empty = {"records": 0, "new": 0, "rows": 0, "skipped": 0, "invalid": 0}
    async with sessions() as session:
        try:
            payload_id, stored = await apple_archive.store_payload(session, body, headers)
            if not stored:
                await session.rollback()
                event(logger, "APPLE_RECEIVER_DUPLICATE", payload_id=payload_id)
                return {"status": "duplicate", **empty}
            try:
                # Up to APPLE_RECEIVER_MAX_BODY_BYTES of JSON: parsed off the event loop.
                result = await anyio.to_thread.run_sync(parse_hae, body, headers)
            except HaeFormatError as exc:
                import_id = await apple_archive.start_import(session, channel=HAE, origin=RECEIVER)
                message = f"Health Auto Export içeriği okunamadı: {exc}"
                await apple_archive.finish_import(
                    session,
                    import_id,
                    error=message,
                    bytes_read=len(body),
                    summary={"payload_id": payload_id},
                )
                await session.commit()
                event(logger, "APPLE_RECEIVER_INVALID", payload_id=payload_id)
                raise ImportRejected(message, import_id=import_id) from exc
            import_id = await apple_archive.start_import(session, channel=HAE, origin=RECEIVER)
            records = list(result.records)
            new = await apple_archive.write_records(
                session, records, payload_id=payload_id, import_id=import_id
            )
            stats = _stats(await normalize_records(session, new)) if new else {}
            summary = {
                "payload_id": payload_id,
                "counts": _jsonable(result.counts),
                "skipped": _jsonable(result.skipped),
                "invalid": _jsonable(result.invalid),
                "normalize": stats,
            }
            await apple_archive.finish_import(
                session,
                import_id,
                bytes_read=len(body),
                records_read=len(records),
                records_new=len(new),
                rows_written=_written(stats),
                summary=summary,
            )
            await session.commit()
        except ImportRejected:
            raise
        except Exception as exc:
            await session.rollback()
            if isinstance(exc, IntegrityError) and not last_attempt:
                raise _LostRace from exc
            await _receiver_failed(sessions, len(body), type(exc).__name__)
            raise
    answer = {
        "status": "ok",
        "records": len(records),
        "new": len(new),
        "rows": _written(stats),
        "skipped": _count(result.skipped),
        "invalid": _count(result.invalid),
    }
    event(
        logger,
        "APPLE_RECEIVER_STORED",
        payload_id=payload_id,
        import_id=import_id,
        records=answer["records"],
        new=answer["new"],
        rows=answer["rows"],
    )
    return answer
