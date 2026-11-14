"""SQLite's test-only SQL and transaction/pool restrictions.

Nothing in the application installs this adapter. Tests opt in from conftest; PostgreSQL
integration tests continue to use the production implementation.
"""

from collections.abc import Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.dialect import DATABASES, PostgreSQL


class SQLite(PostgreSQL):
    value_block_size = 900

    def implementation(self, operation):
        from support.sqlite_operations import OPERATIONS

        return OPERATIONS[f"{operation.__module__}.{operation.__name__}"]

    def setup_scope(self, slots):
        return slots

    insert = staticmethod(insert)
    least = func.min
    greatest = func.max

    def among(self, column, values: Sequence):
        return column.in_(list(values))

    def value_blocks(self, values: Sequence):
        # Old SQLite builds allow 999 parameters; reserve room for the data type.
        for offset in range(0, len(values), self.value_block_size):
            yield values[offset : offset + self.value_block_size]

    def attempt_scope(self, session: AsyncSession, attempts: int):
        # A savepoint's read snapshot cannot upgrade beside another SQLite writer.
        return 1, nullcontext

    async def control(
        self, session: AsyncSession | AsyncConnection, statement, parameters=None
    ) -> None:
        pass

    async def clock(self, session: AsyncSession) -> datetime:
        return datetime.now(UTC)

    async def read_revision(self, session: AsyncSession) -> tuple[object, ...] | None:
        connection = await session.connection()
        raw = (await connection.get_raw_connection()).driver_connection
        # total_changes does not rewind on rollback. Never publish uncommitted data.
        if raw.in_transaction:
            return None
        identity = connection.info.setdefault("history_cache_identity", object())
        version, changes = (
            await session.execute(
                text("SELECT data_version, total_changes() FROM pragma_data_version")
            )
        ).one()
        # data_version detects other connections; total_changes detects this one.
        return (identity, version, changes)

    async def credential_lock(self, session: AsyncSession, namespace: int) -> None:
        await session.execute(text("BEGIN IMMEDIATE"))

    def concurrency(self, requested: int) -> int:
        # In-memory tests share a connection; transactions must not interleave on it.
        return 1

    def pool_options(self, concurrency: int, garmin: bool, *, url) -> dict[str, Any]:
        if url.database in (None, "", ":memory:"):
            return {}
        return super().pool_options(concurrency, garmin, url=url)


def install() -> None:
    from support.sqlite_guards import install as install_schema

    install_schema()
    DATABASES["sqlite"] = SQLite()
