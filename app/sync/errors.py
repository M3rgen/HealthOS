"""Short, stable error summaries for status endpoints and checkpoint rows.

`str(exc)` can carry upstream response text into the UI and the database. Status rows
keep only the exception class name plus a short code when one is known, for example
``GoogleHealthError (http_403)``. Rejected response contents stay in the raw archive.

Response validation failures (``InvalidResponseError``) take precedence over
HTTP status fields: they classify as ``invalid_response``, are never transient
and keep the saved pagination token, whatever code or text the body carried.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy.exc import DBAPIError

from app.core import sqlstate as driver_errors
from app.core.logging import event
from app.google_health.errors import GoogleHealthError, GoogleHealthErrorCode
from app.google_health.oauth import OAuthError
from app.google_health.pagination import RepeatedPageTokenError

logger = logging.getLogger(__name__)

# PostgreSQL's "try the transaction again" failures: never a problem with the data.
DATABASE_CONFLICTS = {
    driver_errors.DEADLOCK_DETECTED: "database_deadlock",
    driver_errors.SERIALIZATION_FAILURE: "database_serialization",
}
CONFLICT_ATTEMPTS = 4
CONFLICT_BACKOFF = 0.05


def database_conflict(exc: BaseException) -> str | None:
    """The error code of a deadlock or serialization failure, else None."""
    if isinstance(exc, DBAPIError):
        return DATABASE_CONFLICTS.get(driver_errors.extract(exc) or "")
    return None


async def retry_database_conflicts[T](attempt: Callable[[], Awaitable[T]]) -> T:
    """Run a whole transaction, retrying it after PostgreSQL aborted it for a deadlock.

    ``attempt`` opens and commits its own transaction (and owns everything it does in it),
    so a retry starts clean. Bounded; the last failure is raised (and classified transient).
    """
    for number in range(1, CONFLICT_ATTEMPTS):
        try:
            return await attempt()
        except DBAPIError as exc:
            code = database_conflict(exc)
            if code is None:
                raise
            event(logger, "DATABASE_CONFLICT_RETRY", error_code=code, attempt=number)
            await asyncio.sleep(CONFLICT_BACKOFF * 2 ** (number - 1))
    return await attempt()


def error_summary(exc: BaseException) -> str:
    code = error_code(exc)
    name = type(exc).__name__
    return f"{name} ({code})" if code else name


def error_code(exc: BaseException) -> str | None:
    if conflict := database_conflict(exc):
        return conflict
    if isinstance(exc, RepeatedPageTokenError):
        return "repeated_page_token"
    if isinstance(exc, (GoogleHealthError, OAuthError)):
        if exc.code is not None:
            return exc.code.value
        if exc.http_status is not None:
            return f"http_{exc.http_status}"
    return None


def is_transient(exc: BaseException) -> bool:
    """Retry only known network, rate-limit, server and database-conflict failures."""
    if isinstance(exc, OAuthError):
        return exc.transient
    if isinstance(exc, httpx.TransportError) or database_conflict(exc):
        return True
    if isinstance(exc, GoogleHealthError):
        if exc.code is not None:
            return exc.code == GoogleHealthErrorCode.CONNECTION
        return exc.http_status == 429 or (
            exc.http_status is not None and 500 <= exc.http_status < 600
        )
    return False
