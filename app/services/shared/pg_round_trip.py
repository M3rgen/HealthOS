"""What a stored PostgreSQL definition may look like after a pg_dump and a restore.

pg_dump prints triggers, views and checks with the server's deparser and the restore parses
that text back. The deparser is not idempotent: a left-deep ``OR`` chain loses its nesting and
``x IN ('a', 'b')`` on ``varchar`` moves the cast onto each element. The same definition is then
spelled differently, so ``healthos verify`` would report a tampered guard on an honest restore.

The expected definition is therefore also fed through the same deparse/parse cycle, by this
server itself, in a savepoint that is rolled back. Only spellings the expected definition
reaches are accepted: anything else (``WHEN (false)``, one value fewer in a list) still differs.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

# A dump of a restored database re-parses once more; a fixed point arrives within a few steps.
_MAX_ROUND_TRIPS = 4
# text() takes ':name' for a bind parameter; a deparsed string literal may contain one.
_NOT_A_CAST = re.compile(r"(?<!:):(?!:)")


def literal_sql(statement: str) -> str:
    """``statement`` escaped for ``sqlalchemy.text`` (colons of string literals are not binds)."""
    return _NOT_A_CAST.sub(r"\\:", statement)


async def round_trip_forms(
    session: AsyncSession, first: str, reparse: Callable[[str], Awaitable[str]]
) -> set[str]:
    """``first`` and every spelling re-parsing it leads to.

    ``reparse`` creates the object from a spelling and returns what the server prints for it;
    it runs in its own savepoint, so a spelling that does not parse just ends the chain.
    """
    forms = {first}
    current = first
    for _ in range(_MAX_ROUND_TRIPS):
        try:
            async with session.begin_nested():
                current = await reparse(current)
        except DBAPIError:
            break
        if current in forms:
            break
        forms.add(current)
    return forms


async def restored_trigger_matches(
    session: AsyncSession, table: str, trigger: str, expected: str, actual: str
) -> bool:
    """Is ``actual`` a spelling a restore gives ``expected``? (Both canonical: ``ON <table>``.)"""
    stand_in = f"healthos_verify_rt_{table}"
    savepoint = await session.begin_nested()
    try:
        await session.execute(text(f"CREATE TEMP TABLE {stand_in} (LIKE {table})"))

        async def reparse(form: str) -> str:
            await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON pg_temp.{stand_in}"))
            await session.execute(
                text(literal_sql(form.replace(f" ON {table} ", f" ON pg_temp.{stand_in} ", 1)))
            )
            printed = await session.scalar(
                text(
                    "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t "
                    "WHERE t.tgrelid = CAST(:table AS regclass) AND t.tgname = :trigger"
                ),
                {"table": f"pg_temp.{stand_in}", "trigger": trigger},
            )
            return " ".join(str(printed).split()).replace(
                f" ON pg_temp.{stand_in} ", f" ON {table} ", 1
            )

        forms = await round_trip_forms(session, expected, reparse)
    finally:
        await savepoint.rollback()
    return actual in forms


async def restored_view_matches(
    session: AsyncSession, view: str, expected: str, actual: str
) -> bool:
    """Is ``actual`` a spelling a restore gives the temporary view ``view`` (now ``expected``)?"""

    async def reparse(form: str) -> str:
        await session.execute(
            text(f"CREATE OR REPLACE TEMP VIEW {view} AS {literal_sql(form.rstrip(' ;'))}")
        )
        printed = await session.scalar(text(f"SELECT pg_get_viewdef('pg_temp.{view}'::regclass)"))
        return " ".join(str(printed).split())

    return actual in await round_trip_forms(session, expected, reparse)


async def restored_check_matches(
    session: AsyncSession, table: str, name: str, expected: str, actual: str
) -> bool:
    """Is ``actual`` a spelling a restore gives the CHECK constraint ``expected``?"""
    if not expected.startswith("CHECK"):
        return False
    stand_in = f"healthos_verify_ck_{table}"
    savepoint = await session.begin_nested()
    try:
        await session.execute(text(f"CREATE TEMP TABLE {stand_in} (LIKE {table})"))

        async def reparse(form: str) -> str:
            await session.execute(
                text(f"ALTER TABLE pg_temp.{stand_in} DROP CONSTRAINT IF EXISTS {name}")
            )
            await session.execute(
                text(f"ALTER TABLE pg_temp.{stand_in} ADD CONSTRAINT {name} {literal_sql(form)}")
            )
            printed = await session.scalar(
                text(
                    "SELECT pg_get_constraintdef(k.oid) FROM pg_constraint k "
                    "WHERE k.conrelid = CAST(:table AS regclass) AND k.conname = :name"
                ),
                {"table": f"pg_temp.{stand_in}", "name": name},
            )
            return str(printed)

        forms = await round_trip_forms(session, expected, reparse)
    finally:
        await savepoint.rollback()
    return actual in forms
