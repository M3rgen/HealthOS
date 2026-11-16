"""Atomic FIT ingestion through the existing raw archive and identity/source chain."""

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fit import FIT_FILE, FIT_SOURCE, MAX_FIT_BYTES
from app.services.archive.storage import store_records

from .fit_decode import FitError
from .fit_records import FitImport, parse_fit
from .garmin_tracks import refresh_import


@dataclass
class FitImportResult:
    sha256: str
    records: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


async def import_fit_bytes(session: AsyncSession, data: bytes) -> FitImportResult:
    """Validate and import bounded FIT bytes; the caller owns the transaction."""
    if len(data) > MAX_FIT_BYTES:
        raise FitError("FIT dosyası çok büyük.")
    parsed = await asyncio.to_thread(parse_fit, data)
    return await import_fit(session, parsed)


async def import_fit(session: AsyncSession, parsed: FitImport) -> FitImportResult:
    """Caller owns the transaction; file bytes and projections commit or roll back together.

    Raw-file bytes keep their existing version/immutability guards. Reuploads reuse
    the content-addressed file row and track package, alongside normal v2 identities
    for sessions/samples and the existing reconciliation and supersession rules. A
    track projection failure is deferred to ``--rebuild`` and never fails the import;
    the upload's workouts are linked to its content for the track API.
    """
    await store_records(session, FIT_FILE, [parsed.file], source=FIT_SOURCE)
    await refresh_import(session, parsed)
    result = FitImportResult(parsed.sha256)
    for spec, records in parsed.records.items():
        for offset in range(0, len(records), 500):
            batch = records[offset : offset + 500]
            stats = await store_records(session, spec, batch, source=FIT_SOURCE)
            result.records += len(batch)
            result.inserted += stats.inserted
            result.updated += stats.updated
            result.unchanged += stats.unchanged
    return result
