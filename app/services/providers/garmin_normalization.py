"""Owned normalization lifecycle and value-free persistent status projections."""

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fetch_window import FetchWindow
from app.core.ingestion_stats import NormalizeStats
from app.core.logging import event
from app.core.timeutil import as_utc
from app.db.contracts import OwnershipLost, TransactionSessions
from app.db.dialect import dialect_insert
from app.models.garmin import GarminNormalizationState
from app.schemas.status import GarminNormalization

from .ingestion_contracts import IngestionPage, IngestionSource

logger = logging.getLogger(__name__)
ERROR_CODE = "garmin_normalization_failed"


async def normalize[Page: IngestionPage](
    source: IngestionSource[Page],
    window: FetchWindow,
    sessions: TransactionSessions,
    *,
    clock: Callable[[], datetime],
    seen_since: datetime | None = None,
) -> NormalizeStats:
    """Keep completed chunks and report failures without releasing the owner's capability.

    Ownership loss and cancellation bypass failure recording entirely. An ordinary failure
    is recorded through the same fence; losing ownership there must never trigger a pooled
    write or replace the original exception. No exception text or arguments leave this stage.
    """
    try:
        stats = await source.normalize(window, sessions=sessions, seen_since=seen_since)
    except OwnershipLost:
        raise
    except Exception:
        try:
            await _mark(sessions, clock(), error=ERROR_CODE)
        except Exception:
            event(logger, "GARMIN_NORMALIZATION_STATUS_WRITE_FAILED", error_code=ERROR_CODE)
        event(logger, "GARMIN_NORMALIZATION_FAILED", error_code=ERROR_CODE)
        raise
    await _mark(sessions, clock(), error=None)
    return stats


async def _mark(sessions: TransactionSessions, now: datetime, *, error: str | None) -> None:
    async with sessions.begin() as session:
        await session.execute(
            dialect_insert(session, GarminNormalizationState)
            .values(id=1)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        state = await session.get(GarminNormalizationState, 1, with_for_update=True)
        if state is None:
            raise RuntimeError("Garmin normalization state row could not be locked")
        state.last_attempt_at = now
        state.last_error_code = error
        if error is None:
            state.last_success_at = now
        else:
            state.last_error_at = now


async def read_status(session: AsyncSession) -> GarminNormalization:
    state = await session.get(GarminNormalizationState, 1)
    # Project only a fixed code, even if an old or damaged database contains free text.
    failing = state is not None and state.last_error_code is not None
    return {
        "status": "degraded" if failing else "healthy" if state else "unknown",
        "last_attempt_at": _iso(state.last_attempt_at if state else None),
        "last_success_at": _iso(state.last_success_at if state else None),
        "last_error_at": _iso(state.last_error_at if state else None),
        "last_error_code": ERROR_CODE if failing else None,
    }


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat().replace("+00:00", "Z") if value else None
