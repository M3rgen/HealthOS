"""Observe a derived-day bump before the transaction that caused it commits."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def drain_invalidations(session: AsyncSession) -> None:
    """PostgreSQL bumps dirty days at commit (migration 0028); SQLite's triggers bump at once.

    Production never reads a derived day in the transaction that wrote its source. A test that
    simulates a committed writer from inside one transaction runs the commit-time step here.
    """
    if (await session.connection()).dialect.name == "postgresql":
        await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
