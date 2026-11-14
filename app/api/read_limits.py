"""HTTP side of request read limits: which requests are limited and what a timeout returns.

Only GET and HEAD reads run under ``API_READ_STATEMENT_TIMEOUT_SECONDS``. The database layer
rolls back and logs ``API_READ_TIMEOUT``; a cancelled read becomes HTTP 504 here (``/health``
reports it as an unavailable database instead).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings
from app.db.contracts import SessionFactory
from app.db.request_sessions import ReadTimedOut, limited_session, read_limit

READ_METHODS = frozenset({"GET", "HEAD"})
TIMEOUT_DETAIL = "Database read timed out. Please try again."


def read_seconds(application: Any, method: str) -> int:
    settings = getattr(application.state, "settings", None) or get_settings()
    return settings.api_read_statement_timeout_seconds if method in READ_METHODS else 0


@asynccontextmanager
async def request_session(
    sessions: SessionFactory, request: Request
) -> AsyncIterator[AsyncSession]:
    """A session from ``sessions`` limited for this request; a timeout raises HTTP 504."""
    try:
        async with limited_session(sessions, read_seconds(request.app, request.method)) as session:
            yield session
    except ReadTimedOut:
        raise HTTPException(504, TIMEOUT_DETAIL) from None


class ReadLimitMiddleware:
    """Set the limit that ``app.db.session.get_session`` reads for each HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = read_limit.set(read_seconds(scope["app"], scope["method"]))
        try:
            await self.app(scope, receive, send)
        finally:
            read_limit.reset(token)


async def read_timed_out(request: Request, exc: Exception) -> Response:
    """``get_session`` raises ``ReadTimedOut``; answer exactly as ``request_session`` does."""
    return await http_exception_handler(request, HTTPException(504, TIMEOUT_DETAIL))


def install(application: FastAPI) -> None:
    application.add_middleware(ReadLimitMiddleware)
    application.add_exception_handler(ReadTimedOut, read_timed_out)
