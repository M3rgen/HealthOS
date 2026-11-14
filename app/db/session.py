"""Database lifecycle and request sessions; importing this module opens no engine.

A caller owns the transaction of every session it passes to a service. Services may
flush or use savepoints, but must not commit/roll back that caller's transaction.
Workers that open their own sessions own those transactions (page/day/job boundaries).
HTTP write handlers commit after the service succeeds; closing a failed request rolls
back. Request reads do not commit. Startup binds the shared factory; CLI invocations
bind it on first use. Externally supplied factories remain owned by their caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.engine import create_shared_engine
from app.db.request_sessions import limited_session, read_limit

SessionLocal = async_sessionmaker(expire_on_commit=False)


def initialize_sessions(settings: Settings, sessions: async_sessionmaker) -> AsyncEngine | None:
    if sessions.kw.get("bind") is not None:
        return None
    engine = create_shared_engine(settings)
    sessions.configure(bind=engine)
    return engine


@asynccontextmanager
async def database_lifespan(settings: Settings, sessions: async_sessionmaker):
    engine = initialize_sessions(settings, sessions)
    try:
        yield sessions
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            finally:
                sessions.configure(bind=None)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI's request session, limited as the API layer set ``read_limit`` for the request."""
    async with limited_session(SessionLocal, read_limit.get()) as session:
        yield session
