"""Transaction-local limits belong only to HTTP read sessions, never shared factories.

This layer knows nothing of HTTP. The API layer decides which requests are limited (it sets
``read_limit`` for FastAPI's ``get_session`` or passes ``seconds``) and turns ``ReadTimedOut``
into its responses.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.util import await_only

from app.core.logging import event as log_event
from app.core.sqlstate import QUERY_CANCELED, extract
from app.db.contracts import SessionFactory
from app.db.dialect import database

logger = logging.getLogger(__name__)
# Seconds for the current request's sessions; 0, the default outside HTTP, sends no control.
read_limit: ContextVar[int] = ContextVar("read_limit", default=0)


class ReadTimedOut(Exception):
    """PostgreSQL cancelled a limited read; the session has already rolled back."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"database read exceeded {seconds} s")
        self.seconds = seconds


@asynccontextmanager
async def limited_session(sessions: SessionFactory, seconds: int) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:

        def limit_transaction(
            sync_session: Session, transaction: SessionTransaction, connection: Connection
        ) -> None:
            # after_begin runs before the first SQL on each connection, including after a
            # commit/rollback. Session.add() may create an ORM transaction synchronously,
            # so wait for this I/O hook. Savepoints inherit the outer transaction's limit.
            if transaction.parent is None:
                # A proxy for the already-open connection; the session keeps ownership.
                target = AsyncConnection(AsyncEngine(connection.engine), connection)
                await_only(
                    database(session).control(
                        target, text(f"SET LOCAL statement_timeout = {seconds * 1000}")
                    )
                )

        if seconds:
            event.listen(session.sync_session, "after_begin", limit_transaction)
        try:
            yield session
        except Exception as exc:
            # Fetches after a stream's first raise the driver's own, unwrapped error.
            if not seconds or extract(exc) != QUERY_CANCELED:
                raise
            # Clear PostgreSQL's aborted transaction before returning the connection.
            await session.rollback()
            log_event(logger, "API_READ_TIMEOUT", statement_timeout_seconds=seconds)
            raise ReadTimedOut(seconds) from None
        finally:
            if seconds:
                event.remove(session.sync_session, "after_begin", limit_transaction)
