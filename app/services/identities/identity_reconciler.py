"""The nightly identity reconciler: ``identity-backfill --incremental`` as a scheduler job.

It runs over every archived data type: incrementally where a full ``identity-backfill`` ran,
else the data type's first full pass (``skipped`` says so). It decides new source chains,
records refuted ones and links the duplicate rows written since its checkpoint
(app/services/identities/identity_backfill.py). What it leaves to the owner (chains to review,
ambiguous
sources, pairs nothing decided, pairs that look like a rename) goes to the log and to
``healthos status``. The result of each data type goes to the log and to
``identity_reconcile_state``, which ``healthos status`` shows. Another run of
identity-backfill holding the single-run lock makes the job skip (logged).

The scheduler starts it when the scheduled Google reconciliations of the night have ended
(app/services/synchronization/scheduler.py), so it rarely meets one. A data type whose lock is still
busy
after ``LOCK_ATTEMPTS`` (a watchdog recovery, a sync by hand) is tried again after all the
others, its batches backing off until the window ends (``until``, 03:55 UTC by default) or,
started after it, for ``wait_minutes`` (app/services/identities/identity_reconcile_window.py). Only
then
does it fail: logged with its error, counted in ``status`` and ``/health``
(``identity_lock_contention``), and settled by the next run from its checkpoint.
"""

from __future__ import annotations

import logging
from datetime import time

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import error_diagnostics, event

from .identity_backfill import identity_backfill
from .identity_reconcile_window import busy_deadline
from .source_chain_locks import BackfillRunning

logger = logging.getLogger(__name__)


class IdentityReconciler:
    def __init__(
        self, sessions: async_sessionmaker, until: time | None = None, wait_minutes: int = 0
    ) -> None:
        self.sessions = sessions
        self.until = until
        self.wait_minutes = wait_minutes

    async def run(self) -> dict | None:
        deadline = busy_deadline(self.until, self.wait_minutes)
        event(
            logger,
            "IDENTITY_RECONCILE_STARTED",
            busy_lock_wait_until=deadline.isoformat() if deadline else None,
        )
        try:
            results = await identity_backfill(
                self.sessions, incremental=True, lock_deadline=deadline
            )
        except BackfillRunning:
            event(logger, "IDENTITY_RECONCILE_SKIPPED", reason="identity-backfill is running")
            return None
        except Exception as exc:
            logger.error(
                "Identity reconciliation failed",
                extra={
                    "event": "IDENTITY_RECONCILE_FAILED",
                    "error_type": type(exc).__name__,
                    **error_diagnostics(exc, frames=True),
                },
            )
            return None
        summary = {name: stats.summary() for name, stats in results.items()}
        for name, values in summary.items():
            event(logger, "IDENTITY_RECONCILE_DATA_TYPE", data_type=name, **values)
        event(
            logger,
            "IDENTITY_RECONCILE_COMPLETED",
            data_types=len(summary),
            superseded=sum(values["superseded"] for values in summary.values()),
            chains_decided=sum(values["chains_decided"] for values in summary.values()),
            ambiguous=sum(values["ambiguous"] for values in summary.values()),
            review_suggested=sum(values["review_suggested"] for values in summary.values()),
            review_distinct=sum(values["review_distinct"] for values in summary.values()),
            failed=sum(1 for values in summary.values() if values["error"]),
            kept_apart_undecided=sum(values["kept_apart_undecided"] for values in summary.values()),
            skipped=sum(1 for values in summary.values() if values["skipped"]),
        )
        return summary
