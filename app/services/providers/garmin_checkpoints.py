"""Garmin pull bookkeeping: per-kind state rows and the backfill checkpoint.

Every write goes through the owner's fenced transactions (``Fenced``): a lost connection is
lost ownership, never recorded as a kind's own failure. The state row is created with an
upsert and then locked, so the first attempt at a kind cannot race its own first row, and the
checkpoint only moves while the row still holds this run's bounds.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import error_diagnostics, event
from app.db.contracts import OwnershipLost, TransactionSessions
from app.db.dialect import dialect_insert
from app.garmin.client import GarminError
from app.models.garmin import GarminPullState

logger = logging.getLogger(__name__)


class Fenced:
    """An owner's transactions; a lost connection is lost ownership, never a kind's failure."""

    def __init__(self, sessions: TransactionSessions) -> None:
        self.sessions = sessions

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self.sessions.begin() as session:
                yield session
        except DBAPIError as exc:
            if exc.connection_invalidated:
                raise OwnershipLost("Garmin ownership connection was lost") from exc
            raise


class Checkpoints:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    async def _state(self, session: AsyncSession, kind: str) -> GarminPullState:
        # The first attempt at a kind may race its first row; the row lock serializes writers.
        await session.execute(
            dialect_insert(session, GarminPullState)
            .values(kind=kind)
            .on_conflict_do_nothing(index_elements=["kind"])
        )
        state = await session.get(GarminPullState, kind, with_for_update=True)
        if state is None:
            raise RuntimeError("Garmin pull state row could not be locked")
        return state

    async def mark(
        self, sessions: TransactionSessions, kind: str, *, success: bool, error: str | None = None
    ) -> None:
        now = self.clock()
        async with sessions.begin() as session:
            state = await self._state(session, kind)
            state.last_attempt_at = now
            if success:
                state.last_success_at = now
            else:
                state.last_error = (error or "unknown")[:2000]
                state.last_error_at = now

    async def record_failure(
        self, sessions: TransactionSessions, kind: str, exc: Exception, name: str
    ) -> str:
        """Record a kind's failure; a lost owner writes nothing, the failure is still reported."""
        if isinstance(exc, GarminError):
            error = str(exc)
            details: dict[str, Any] = {"error": error}
        else:
            details = error_diagnostics(exc)
            error = details.get("error_type", type(exc).__name__)
        try:
            await self.mark(sessions, kind, success=False, error=error)
        except OwnershipLost:
            pass
        event(logger, name, kind=kind, **details)
        return error

    async def checkpoint(
        self, sessions: TransactionSessions, kind: str, since: date, until: date
    ) -> date | None:
        """The day to start ``kind``'s backfill from: the stored ``backfill_next`` when the
        bounds match (``None`` when those bounds already finished), else ``since`` anew."""
        async with sessions.begin() as session:
            state = await self._state(session, kind)
            if state.backfill_since == since and state.backfill_until == until:
                return state.backfill_next
            state.backfill_since = since
            state.backfill_until = until
            state.backfill_next = since
            return since

    async def advance(
        self, sessions: TransactionSessions, kind: str, since: date, until: date, done_through: date
    ) -> None:
        """Move this run's checkpoint; a row now holding other bounds is another run's."""
        following = done_through + timedelta(days=1)
        async with sessions.begin() as session:
            result = await session.execute(
                update(GarminPullState)
                .where(
                    GarminPullState.kind == kind,
                    GarminPullState.backfill_since == since,
                    GarminPullState.backfill_until == until,
                )
                .values(backfill_next=following if following <= until else None)
            )
            if not cast(CursorResult[Any], result).rowcount:
                raise OwnershipLost("Garmin backfill bounds belong to another run")
