"""How long the identity reconciler waits for a busy data type, and what is left.

A reconciler batch backs off when another writer holds its data type (``_exclusively`` in
app/services/identities/identity_backfill.py): ``LOCK_ATTEMPTS`` times, about four minutes. Any long
writer
holds it: the daily or weekly Google reconciliation, the watchdog's recovery after a restart, a
sync or backfill by hand. So a data type still busy after its first attempts is tried again
after the others (``waiting_until``) until a deadline: the end of the nightly window
(``IDENTITY_RECONCILE_UNTIL_*``, 03:55 UTC) for the nightly job started before it, else
``IDENTITY_RECONCILE_LOCK_WAIT_MINUTES`` from the start (a run by hand, a nightly job started
late; 0 keeps the ``LOCK_ATTEMPTS`` alone). The per-attempt contract stays the same: try for
the lock without queueing for 0.1 s, queue at most 1 s, wait at most 2 s for a row lock, then
back off (2, 4 ... 60 s). The deadline bounds the retries, never a lock held longer.

``failed_nights`` and ``lock_contention`` read ``identity_reconcile_state`` for
``healthos status`` and ``/health``: a data type whose last run failed, and how many UTC
nights it has gone without a successful run since its checkpoint.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, time, timedelta

from app.core.timeutil import as_utc
from app.models.identity_tables import IdentityReconcileState

# What LockUnavailable says (app/services/identities/identity_backfill.py); status and /health find
# it.
LOCK_ERROR = "could not get the identity lock"

_deadline: ContextVar[datetime | None] = ContextVar("identity_lock_deadline", default=None)


def _now() -> datetime:
    return datetime.now(UTC)


def busy_deadline(
    until: time | None, wait_minutes: int, now: datetime | None = None
) -> datetime | None:
    """Until when a busy data type is retried: today's window end (UTC, ``until``) while it is
    still ahead, else ``wait_minutes`` from now; ``None`` (``LOCK_ATTEMPTS`` alone) for 0."""
    now = as_utc(now) or _now()
    if until is not None:
        end = datetime.combine(now.date(), until, tzinfo=UTC)
        if now < end:
            return end
    return now + timedelta(minutes=wait_minutes) if wait_minutes > 0 else None


@contextmanager
def waiting_until(deadline: datetime | None) -> Iterator[None]:
    """Batches in this context keep retrying a busy lock until ``deadline``."""
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def current_deadline() -> datetime | None:
    return _deadline.get()


def keep_trying(attempts_made: int, attempts: int) -> bool:
    """Whether a batch that failed ``attempts_made`` times tries again."""
    if attempts_made < attempts:
        return True
    deadline = _deadline.get()
    return deadline is not None and _now() < deadline


def lock_failed(error: str | None) -> bool:
    return bool(error) and LOCK_ERROR in (error or "")


def failed_nights(state: IdentityReconcileState) -> int | None:
    """UTC nights the data type has gone without a successful run while its last run failed
    (``None`` when it did not fail): the dates after its checkpoint up to its last start, at
    least one. One nightly run a night makes it the number of nights in a row it failed."""
    if not state.last_error:
        return None
    started = as_utc(state.last_started_at)
    settled = as_utc(state.checkpoint_at)
    if started is None or settled is None:
        return 1
    return max(1, (started.date() - settled.date()).days)


def lock_contention(states: Sequence) -> list[dict]:
    """Data types whose last run gave up on a busy lock, for ``/health``."""
    return [
        {
            "data_type": state.data_type,
            "nights": failed_nights(state),
            "since": _iso(as_utc(state.checkpoint_at)),
            "last_attempt": _iso(as_utc(state.last_started_at)),
        }
        for state in sorted(states, key=lambda item: item.data_type)
        if lock_failed(state.last_error)
    ]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None
