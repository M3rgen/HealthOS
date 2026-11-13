"""Session capabilities shared by services and ownership-bound transactions."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class OwnershipLost(RuntimeError):
    """The run may no longer write; leave the last committed checkpoint resumable."""


class TransactionSessions(Protocol):
    """Short atomic writes, including factories that verify a run's lock."""

    def begin(self) -> AbstractAsyncContextManager[AsyncSession]: ...


class SessionFactory(TransactionSessions, Protocol):
    """Read sessions plus short transactions; implemented by async_sessionmaker."""

    def __call__(self) -> AsyncSession: ...


class RunOwnership(Protocol):
    """Exclusive ownership of one named run: transactions fenced by the owner's lock."""

    def __call__(self, key: str) -> AbstractAsyncContextManager[TransactionSessions]: ...


def unlocked_for_tests(sessions: TransactionSessions) -> RunOwnership:
    """Explicitly bypass ownership for tests; never use for production ingestion."""

    @asynccontextmanager
    async def unlocked(key: str) -> AsyncIterator[TransactionSessions]:  # noqa: ARG001
        yield sessions

    return unlocked
