"""Run ownership, separate from transaction-scoped identity locks.

A PostgreSQL owner uses one checked-out connection for both its session advisory lock and
all short page transactions. Coroutines of this process queue for a key on a local gate and
hold no connection meanwhile. Only the head of a key's queue goes to the database; when
another process owns the key, it keeps its one connection and queues in the server's lock
manager in short slices: the lock passes in request order, no new connection is opened per
retry and no snapshot is held for the whole wait. Checking the lock inside every transaction
prevents SQLAlchemy reconnecting a lost connection and continuing to write without ownership.
No transaction spans Google I/O.

An owner slot is taken only around a held lock, so a queue on a busy key never occupies one:
waiting for one data type cannot hold up another, and the limit counts the runs actually
executing. Everything here is backend-neutral except :class:`_RunLock`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db import connections
from app.db.contracts import OwnershipLost, RunOwnership, TransactionSessions
from app.db.dialect import database_operation
from app.db.postgres import (
    GARMIN_NAMESPACE,
    LOCK_NOT_AVAILABLE,
    RUN_NAMESPACE,
    WATCHDOG_NAMESPACE,
    advisory_key,
    sqlstate,
)
from app.db.postgres import NOTIFICATION_NAMESPACE as NOTIFICATION_NAMESPACE
from app.sync.local_lock import LocalLock

# Preserve the ownership module interface for existing callers.
discard_connection = connections.discard_connection

TRY_LOCK = text("SELECT pg_try_advisory_lock(:namespace, :key)")
WAIT_LOCK = text("SELECT pg_advisory_lock(:namespace, :key)")
# A slice that times out leaves the queue only for one round trip before requeueing.
WAIT_SLICE = text("SET LOCAL lock_timeout = '2s'")
HELD_LOCK = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
    "AND pid = pg_backend_pid() AND classid = :namespace AND objid = :unsigned_key "
    "AND objsubid = 2 AND granted AND mode = 'ExclusiveLock')"
)
FIRST_BACKOFF = 0.05
MAX_BACKOFF = 2.0


class _Quota:
    """One namespace's limits: running owners, and connections parked behind another process."""

    __slots__ = ("owners", "parked")

    def __init__(self, limit: int) -> None:
        self.owners = asyncio.Semaphore(limit)
        self.parked = asyncio.Semaphore(limit)


_slots: WeakKeyDictionary[AsyncEngine, dict[int, _Quota]] = WeakKeyDictionary()


def _quota(engine: AsyncEngine, namespace: int, default: int = 1) -> _Quota:
    quotas = _slots.setdefault(engine, {})
    if namespace not in quotas:
        limit = engine.get_execution_options().get("ownership_concurrency", default)
        single = namespace in (WATCHDOG_NAMESPACE, GARMIN_NAMESPACE)
        quotas[namespace] = _Quota(1 if single else limit)
    return quotas[namespace]


async def _contended() -> None:
    """Runs once when a waiting acquisition finds the lock taken; tests observe it."""


class OwnedSessions:
    """A transaction factory bound to the lock's exact connection, never the pool."""

    def __init__(self, connection: AsyncConnection, arguments: dict):
        self.connection = connection
        self.arguments = {
            "namespace": arguments.get("namespace", RUN_NAMESPACE),
            "unsigned_key": arguments["key"] % 2**32,
        }
        self.factory = async_sessionmaker(connection, expire_on_commit=False)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        if self.connection.closed or self.connection.invalidated:
            raise OwnershipLost("sync ownership connection was lost")
        async with self.factory.begin() as session:
            if not await session.scalar(HELD_LOCK, self.arguments):
                raise OwnershipLost("sync ownership lock was lost")
            yield session


class CheckedSessions:
    """Check a caller's ownership before each short data-type transaction.

    The check finishes before the transaction starts. A webhook takes notification
    ownership first, then data-type ownership; neither recovery path takes both locks.
    In particular, a disconnected notification waiting for a data type cannot fetch.
    """

    def __init__(self, sessions: TransactionSessions, check: Callable[[], Awaitable[None]]):
        self.sessions = sessions
        self.check = check

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        await self.check()
        async with self.sessions.begin() as session:
            yield session


@asynccontextmanager
async def ownership_connection(engine: AsyncEngine, namespace: int, *, wait: bool = True):
    """Bound long-lived owners while leaving room for nested work and HTTP archives.

    Notifications and data-type runs have independent quotas: a notification can
    wait for a data type without exhausting its quota. The shared application pool
    reserves both quotas plus watchdog ownership and ordinary request capacity.
    """
    quota = _quota(engine, namespace).owners
    if not wait and quota.locked():
        yield None
        return
    async with quota:
        connection = await engine.connect()
        try:
            yield connection
        finally:
            await discard_connection(connection)


class _RunLock(Protocol):
    """What differs between backends; the queueing around it is written once, below."""

    name: tuple[str, int, str]
    default_limit: int  # the owner limit of an engine that does not set one

    async def attempt(self) -> bool: ...

    async def wait(self) -> None: ...

    async def release(self) -> None: ...

    def scoped(self, sessions: Any) -> Any: ...


class _PostgresLock:
    default_limit = 1

    def __init__(self, engine: AsyncEngine, namespace: int, data_type: str) -> None:
        self.engine = engine
        self.arguments = {"namespace": namespace, "key": advisory_key(data_type)}
        # One gate per engine: separate engines stand for separate processes.
        self.name = (f"postgres:{id(engine)}", namespace, data_type)
        self.connection: AsyncConnection | None = None

    async def attempt(self) -> bool:
        if self.connection is None:
            self.connection = await self.engine.connect()
        async with self.connection.begin():
            return bool(await self.connection.scalar(TRY_LOCK, self.arguments))

    def _connected(self) -> AsyncConnection:
        if self.connection is None:
            raise RuntimeError("the lock has no connection before its first attempt")
        return self.connection

    async def wait(self) -> None:
        connection = self._connected()
        while True:
            try:
                # Session locks survive the commit; the slice only bounds the snapshot.
                async with connection.begin():
                    await connection.execute(WAIT_SLICE)
                    await connection.execute(WAIT_LOCK, self.arguments)
                return
            except DBAPIError as exc:
                if sqlstate(exc) != LOCK_NOT_AVAILABLE:
                    raise

    async def release(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            await discard_connection(connection)

    def scoped(self, sessions: Any) -> OwnedSessions:
        return OwnedSessions(self._connected(), self.arguments)


@database_operation
def _run_lock(engine: AsyncEngine, namespace: int, key: str) -> _RunLock:
    return _PostgresLock(engine, namespace, key)


def garmin_ownership(sessions: async_sessionmaker) -> RunOwnership:
    """Ownership of one Garmin kind (or ``normalize``); waits like a Google data-type run."""

    @asynccontextmanager
    async def owned(key: str) -> AsyncIterator[TransactionSessions]:
        async with run_ownership(sessions, key, namespace=GARMIN_NAMESPACE) as held:
            if held is None:
                raise OwnershipLost("Garmin run ownership was not acquired")
            yield held

    return owned


@asynccontextmanager
async def run_ownership(
    sessions: async_sessionmaker,
    data_type: str,
    *,
    wait: bool = True,
    namespace: int = RUN_NAMESPACE,
    wait_for_slot: bool = False,
):
    """Yield owned transactions, or None for a busy nonblocking attempt.

    ``wait=False`` never queues behind another owner of the key. With ``wait_for_slot`` a
    full limit is a queue, not a refusal: it waits for a free slot (holding no connection)
    and only then tries the key, so None means that the key really has another owner.
    """
    engine = sessions.kw["bind"]
    if not isinstance(engine, AsyncEngine):
        raise TypeError("sync ownership requires an engine-bound session factory")
    lock = _run_lock(engine, namespace, data_type)
    quota = _quota(engine, namespace, lock.default_limit)
    async with _local_gate(lock.name, wait) as (entered, announced):
        if not entered:
            yield None
            return
        try:
            if await _acquire(quota, lock, wait, announced, wait_for_slot):
                try:
                    yield lock.scoped(sessions)
                finally:
                    quota.owners.release()
            else:
                yield None
        finally:
            await lock.release()


async def _acquire(
    quota: _Quota, lock: _RunLock, wait: bool, announced: bool, wait_for_slot: bool = False
) -> bool:
    """Take the lock and exactly one owner slot; on False or an error, no slot is kept.

    The slot is held only around an attempt and from the grant on, never while queueing, so
    a type another process owns neither consumes the limit nor delays a free type. Waiting
    for the lock keeps a connection under ``quota.parked``; past that bound the head of a
    key retries with backoff instead of queueing behind other keys.
    """
    delay = FIRST_BACKOFF
    while True:
        if not wait and not wait_for_slot and quota.owners.locked():
            return False
        await quota.owners.acquire()
        try:
            if await lock.attempt():
                return True
        except BaseException:
            quota.owners.release()
            raise
        quota.owners.release()
        if not wait:
            return False
        if not announced:
            announced = True
            await _contended()
        if quota.parked.locked():
            await lock.release()
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, MAX_BACKOFF)
            continue
        async with quota.parked:  # held until the slot: a granted lock keeps its connection
            await lock.wait()
            await quota.owners.acquire()  # granted: this run now counts against the limit
        return True


# Entries exist only while held or awaited, so per-notification keys never accumulate.
_local_locks: dict[tuple[str, int, str], LocalLock] = {}


@asynccontextmanager
async def _local_gate(name: tuple[str, int, str], wait: bool):
    """Queue this process's owners of one key in request order, without a connection.

    Yields (entered, announced): whether this coroutine may go on to the lock itself, and
    whether it already reported contention (``_contended`` runs once per acquisition).
    """
    entry = _local_locks.get(name)
    if entry is None:
        entry = _local_locks[name] = LocalLock()
    entry.users += 1
    try:
        contended = entry.users > 1
        if contended:
            if not wait:
                yield False, True
                return
            await _contended()
        async with entry.lock:
            yield True, contended
    finally:
        entry.users -= 1
        if entry.users == 0 and _local_locks.get(name) is entry:
            del _local_locks[name]
