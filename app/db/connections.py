"""Cancellation-safe disposal of connections carrying session-scoped state."""

import asyncio

from sqlalchemy.ext.asyncio import AsyncConnection


async def discard_connection(connection: AsyncConnection) -> None:
    """Close the physical connection even when shutdown cancels repeatedly.

    Closing the connection releases its session locks, including when acquisition or a
    commit was cancelled after PostgreSQL granted one. A locked physical connection
    must never return to the shared pool; the engine itself remains available.
    """
    cleanup = asyncio.create_task(_close_connection(connection))
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


async def _close_connection(connection: AsyncConnection) -> None:
    try:
        await connection.invalidate()
    finally:
        await connection.close()
