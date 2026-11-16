"""Catch a data type up after a webhook, without re-fetching on every notification.

Google notifies the physical interval of a changed record, but may keep naming one record's
interval (narrowed or behind) while another record of the same type, uploaded after the
last full fetch, lies outside it. Fetching only the notified range left such a record for
the next reconciliation, hours later. After its own ranges, a processed notification also
fetches the type's incremental window (``incremental_start``: the last successful sync minus
the type's safety window, up to now), unless a completed fetch that reached back that far
already began within ``CATCH_UP_INTERVAL``. The evidence is the append-only retrieval
receipts: a reconciliation, a manual sync or an earlier catch-up all count; a notified range
never does, since it does not reach back to the window's start.

The scheduled catch-up (app/sync/scheduled_catch_up.py) uses the same evidence over its own
interval, so while notifications arrive and their catch-ups keep the window covered it sends no
request. It also asks that the covering fetch reached the recent past: a window that ended long
ago (an early slice of the weekly reconciliation, or a notified range of an old record) never
covers new uploads.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import exists, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ingestion_stats import StoreStats
from app.core.logging import event
from app.core.provenance import provenance
from app.models.retrieval import RetrievalEvent, RetrievalReceipt
from app.sync.errors import error_code

logger = logging.getLogger(__name__)

# Google repeated sleep notifications every ~14 minutes in the incident this answers; at
# most one catch-up per type per half hour keeps the extra requests to one in three such
# notifications, and a late upload waits at most this long plus one notification.
CATCH_UP_INTERVAL = timedelta(minutes=30)


class TypeBusy(RuntimeError):
    """A nonblocking catch-up found the data type's run ownership taken."""


class CatchUp(Protocol):
    """IncrementalSync.catch_up: ``None`` when a recent complete fetch covered the window."""

    async def catch_up(
        self, data_type: str, *, check_owner: Callable[[], Awaitable[None]] | None = None
    ) -> StoreStats | None: ...


async def recently_covered(
    session: AsyncSession,
    data_type: str,
    start: datetime,
    now: datetime,
    within: timedelta | None = None,
) -> bool:
    """A completed sync fetch of ``data_type`` from ``start`` or earlier began recently.

    Recently is CATCH_UP_INTERVAL; a scheduled catch-up passes its own ``within``, and then the
    fetch must also have reached that recent past.
    """
    since = now - (within or CATCH_UP_INTERVAL)
    reached = RetrievalReceipt.requested_to >= since if within is not None else true()
    completed = exists().where(
        RetrievalEvent.receipt_id == RetrievalReceipt.id, RetrievalEvent.kind == "completed"
    )
    found = await session.scalar(
        select(RetrievalReceipt.id)
        .where(
            RetrievalReceipt.lane == "sync",
            RetrievalReceipt.owner == data_type,
            RetrievalReceipt.started_at >= since,
            RetrievalReceipt.requested_from <= start,
            reached,
            completed,
        )
        .limit(1)
    )
    return found is not None


async def catch_up_after_notification(
    incremental: CatchUp,
    data_type: str,
    element_hash: str,
    check_owner: Callable[[], Awaitable[None]],
) -> None:
    """Best effort, after the notification completed: its own ranges were fetched.

    A failure is logged and left to the next notification or reconciliation; the failed
    sync state and the receipt's failed outcome record it like any other fetch.
    """
    try:
        with provenance(source="webhook", context_id=f"webhook:{element_hash}"):
            stats = await incremental.catch_up(data_type, check_owner=check_owner)
    except Exception as exc:
        logger.warning(
            "Webhook catch-up failed",
            extra={
                "event": "WEBHOOK_CATCH_UP_FAILED",
                "data_type": data_type,
                "error_type": type(exc).__name__,
                "error_code": error_code(exc),
            },
        )
        return
    if stats is None:
        event(logger, "WEBHOOK_CATCH_UP_SKIPPED", data_type=data_type)
    else:
        event(
            logger,
            "WEBHOOK_CATCH_UP_COMPLETED",
            data_type=data_type,
            records=stats.inserted + stats.updated,
        )
