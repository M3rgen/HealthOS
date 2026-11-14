"""What a test that traces statements or measures memory sees on PostgreSQL but not on SQLite.

Readers send ``plan_each_query``'s transaction-local plan setting before they read;
HTTP reads also set the default transaction-local statement timeout. The SQLite adapter
skips both. Read assertions leave out exactly these two statements and nothing else.

On asyncpg the driver holds more of a read than ``yield_per`` asks for, by a fixed amount
that does not grow with the archive: SQLAlchemy's server-side cursor fetches at least
``PREFETCH`` rows whenever its buffer runs empty (``AsyncAdapt_asyncpg_ss_cursor``), and
asyncio receives the socket in reads of up to ``SOCKET_READ`` bytes, filled by timing, of
which asyncpg keeps the current and the previous one (``ReadBuffer._buf0``, ``_buf0_prev``).
"""

from typing import Any

PLAN_CONTROL = "SET LOCAL plan_cache_mode = force_custom_plan"
READ_TIMEOUT_CONTROL = "SET LOCAL statement_timeout = 25000"
PREFETCH = 50
SOCKET_READ = 256 * 1024


def is_plan_control(statement: str) -> bool:
    return statement.strip() == PLAN_CONTROL


def is_read_control(statement: str) -> bool:
    return statement.strip() in {PLAN_CONTROL, READ_TIMEOUT_CONTROL}


def read_statements(statements: list[str]) -> list[str]:
    return [statement for statement in statements if not is_read_control(statement)]


def only_reads(conn, cursor, statement, parameters, context, executemany) -> None:
    """Every statement is a SELECT or one of the two exact read controls."""
    assert is_read_control(statement) or statement.lstrip().upper().startswith("SELECT")


def _asyncpg(target: Any) -> bool:
    """``target`` is a session, a session factory or an engine."""
    if hasattr(target, "get_bind"):
        target = target.get_bind()
    elif hasattr(target, "kw"):
        target = target.kw["bind"]
    return target.dialect.driver == "asyncpg"


def rows_in_flight(target: Any, batch: int) -> int:
    """Decoded rows a ``yield_per=batch`` read can hold at once: the batch being consumed and
    the driver's next fetch (two batches on SQLite)."""
    return batch + (max(batch, PREFETCH) if _asyncpg(target) else batch)


def socket_buffers(target: Any) -> int:
    """Received socket data the driver may hold beside the rows (none on SQLite)."""
    return 2 * SOCKET_READ if _asyncpg(target) else 0
