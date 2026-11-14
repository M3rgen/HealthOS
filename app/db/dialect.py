"""Database operations. Production rejects sqlite; tests register their own adapter.

Keep backend selection here, rather than teaching archive and application services about
test-only SQL, parameter limits, pool or transaction restrictions.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from functools import wraps
from inspect import signature
from typing import Any

from sqlalchemy import any_, bindparam, func, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.postgres import CREDENTIAL_LOCK_KEY


class PostgreSQL:
    """The supported runtime database and its transaction-local controls."""

    def implementation(self, operation: Callable) -> Callable:
        """PostgreSQL operations run unchanged; test adapters supply explicit alternatives."""
        return operation

    def setup_scope(self, slots):
        return None

    insert = staticmethod(insert)
    least = func.least
    greatest = func.greatest

    def among(self, column, values: Sequence):
        return column == any_(bindparam(None, list(values), type_=ARRAY(column.type)))

    def value_blocks(self, values: Sequence):
        if values:
            yield values

    def attempt_scope(self, session: AsyncSession, attempts: int):
        return attempts, session.begin_nested

    async def control(
        self, session: AsyncSession | AsyncConnection, statement, parameters=None
    ) -> None:
        """Apply PostgreSQL transaction settings or refresh planner statistics."""
        await session.execute(statement, parameters)

    async def clock(self, session: AsyncSession) -> datetime:
        return await session.scalar(text("SELECT clock_timestamp()"))

    async def read_revision(self, session: AsyncSession) -> tuple[object, ...] | None:
        """The MVCC visibility of *all* committed writers, including other processes.

        A snapshot includes in-flight XIDs: their later commit must invalidate a hit
        even when no new XID was allocated. Own writes are invisible to that marker,
        so a transaction with an assigned XID must bypass the cache. Keep unusually
        large active-transaction lists out of the bounded process cache as well.
        XIDs are cluster-wide: unrelated databases can cause conservative misses too.
        """
        snapshot, writing = (
            await session.execute(
                text("SELECT pg_current_snapshot()::text, pg_current_xact_id_if_assigned()::text")
            )
        ).one()
        if writing is not None or len(snapshot) > 4096:
            return None
        return (session.get_bind(), snapshot)

    async def credential_lock(self, session: AsyncSession, namespace: int) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
            {"namespace": namespace, "key": CREDENTIAL_LOCK_KEY},
        )

    def concurrency(self, requested: int) -> int:
        return requested

    def pool_options(self, concurrency: int, garmin: bool, *, url) -> dict[str, Any]:
        # Notification/type owners and waiters, watchdog, OAuth, archive and API reads.
        return {"pool_size": 3 * concurrency + 3 + (2 if garmin else 0), "max_overflow": 0}


# Only PostgreSQL is installed by the application. Test adapters live under tests/support.
DATABASES: dict[str, PostgreSQL] = {"postgresql": PostgreSQL()}


def for_dialect(name: str) -> PostgreSQL:
    try:
        return DATABASES[name]
    except KeyError:
        raise RuntimeError(f"Unsupported database dialect: {name}") from None


def for_bind(bind) -> PostgreSQL:
    return for_dialect(bind.dialect.name)


def database(target: Any) -> PostgreSQL:
    """Resolve SQL primitives and decorated operations through the same binding rules.

    Sessions and Alembic operations expose get_bind(); session factories keep their
    engine in kw. Engines and connections already carry their dialect. An unbound
    factory uses the production implementation, which can handle a dry run without
    opening a connection. A bound target always goes through the dialect registry.
    """
    if hasattr(target, "get_bind"):
        bind = target.get_bind()
    elif hasattr(target, "kw"):
        bind = target.kw.get("bind")
    else:
        bind = target
    if bind is None:
        return DATABASES["postgresql"]
    return for_bind(bind)


def dialect_insert(session: AsyncSession, table):
    return database(session).insert(table)


def database_operation[**P, R](operation: Callable[P, R]) -> Callable[P, R]:
    """Dispatch a database-specific operation through the same adapter as SQL primitives.

    The first argument is a session, a session factory, an engine or migration operations.
    The default adapter always executes the PostgreSQL implementation. Tests install
    alternate implementations; these do not enter the production import graph.
    """

    first_parameter = next(iter(signature(operation).parameters))

    @wraps(operation)
    def dispatch(*args: P.args, **kwargs: P.kwargs) -> R:
        target: Any = args[0] if args else kwargs[first_parameter]
        return database(target).implementation(operation)(*args, **kwargs)

    return dispatch
