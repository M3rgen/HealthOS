"""File and in-memory ownership for the SQLite unit lane only."""

import asyncio
import fcntl
import sys
import zlib
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.sync.ownership import FIRST_BACKOFF, MAX_BACKOFF, RUN_NAMESPACE


class _SqliteLock:
    """In-process ordering comes from the gate; data types also flock across processes.

    SQLite has a single writer, and a notification claim is an atomic status update,
    so the notification namespace needs no sidecar file per element. A plain SQLite engine
    has no pool to protect, so only an engine that sets a limit is held to one.
    """

    default_limit = sys.maxsize

    def __init__(self, engine: AsyncEngine, namespace: int, key: str) -> None:
        database = engine.url.database or ":memory:"
        self.database = None if database == ":memory:" or namespace != RUN_NAMESPACE else database
        self.key = key
        place = f"memory:{id(engine)}" if database == ":memory:" else _resolved(database)
        self.name = (place, namespace, key)
        self.handle = None

    async def attempt(self) -> bool:
        if self.database is None:
            return True
        if self.handle is None:
            self.handle = _sqlite_file(self.database, self.key)
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    async def wait(self) -> None:
        delay = FIRST_BACKOFF
        while not await self.attempt():
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)

    async def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

    def scoped(self, sessions: Any) -> Any:
        return sessions


def _resolved(database: str) -> str:
    return str(Path(database).resolve())


def _sqlite_file(database: str, data_type: str):
    # A sidecar remains on disk: unlinking it would let a waiter lock an old inode.
    path = Path(database).resolve()
    key = zlib.crc32(data_type.encode())
    return path.with_name(f"{path.name}.sync-{key:08x}.lock").open("a")
