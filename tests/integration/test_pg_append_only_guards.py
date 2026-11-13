"""Every table the archive declares append-only really refuses change, on PostgreSQL.

The other guard tests compare trigger text with the models. This one asks the migrated database:
a row goes in, then UPDATE, DELETE and TRUNCATE must be refused, even in a replica session (the
setting that silences every trigger not marked ENABLE ALWAYS).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.guards import definitions

_, _, PROTECTIVE_TRIGGERS = definitions()

pytestmark = pytest.mark.integration

# What each guard's name promises; other guards allow narrow updates and have their own tests.
_REFUSALS = {
    "append_only": {"UPDATE", "DELETE"},
    "no_delete": {"DELETE"},
    "no_truncate": {"TRUNCATE"},
}
REFUSING: dict[str, set[str]] = {}
for _table, _trigger, _function in PROTECTIVE_TRIGGERS:
    _suffix = _trigger.removeprefix(f"{_table}_")
    if _function == "healthos_refuse_change" and _suffix in _REFUSALS:
        REFUSING.setdefault(_table, set()).update(_REFUSALS[_suffix])

# Values the generic filler cannot guess (each comes from a CHECK constraint).
VALUES = {
    "manual_measurement_flag_events": {"action": "'mark'"},
    "retrieval_events": {"kind": "'page'"},
    "raw_source_chains": {"from_fingerprint": "'a'", "to_fingerprint": "'b'"},
    "raw_source_pairs": {"first_fingerprint": "'a'", "second_fingerprint": "'b'"},
    "raw_source_agreements": {"first_fingerprint": "'a'", "second_fingerprint": "'b'"},
    "raw_source_decisions": {
        "first_fingerprint": "'a'",
        "second_fingerprint": "'b'",
        "decision": "'link'",
    },
    "webhook_events": {"raw_json": "'{}'"},
}


def _literal(data_type: str, enum_label: str | None, max_length: int | None) -> str:
    if enum_label is not None:
        return f"'{enum_label}'"
    if data_type == "uuid":
        return "gen_random_uuid()"
    if "timestamp" in data_type:
        return "'2026-01-01T00:00:00Z'"
    if data_type == "date":
        return "'2026-01-01'"
    if data_type == "boolean":
        return "false"
    if data_type in {"integer", "bigint", "smallint", "numeric", "double precision", "real"}:
        return "1"
    if data_type == "jsonb" or data_type == "json":
        return "'{}'"
    if data_type == "bytea":
        return "'\\x00'"
    return "'" + "synthetic"[: max_length or 9] + "'"


async def _insert_row(connection, table: str) -> str:
    columns = (
        await connection.execute(
            text(
                """
                SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                       c.character_maximum_length,
                       (SELECT e.enumlabel FROM pg_enum e
                         JOIN pg_type t ON t.oid = e.enumtypid
                        WHERE t.typname = c.udt_name
                        ORDER BY e.enumsortorder LIMIT 1) AS enum_label
                FROM information_schema.columns c
                WHERE c.table_name = :table AND c.table_schema = 'public'
                ORDER BY c.ordinal_position
                """
            ),
            {"table": table},
        )
    ).all()
    assert columns, f"{table} is not in the migrated schema"
    fixed = VALUES.get(table, {})
    required = [
        c
        for c in columns
        if c.column_name in fixed or (c.is_nullable == "NO" and c.column_default is None)
    ]
    names = ", ".join(c.column_name for c in required)
    values = ", ".join(
        fixed.get(c.column_name) or _literal(c.data_type, c.enum_label, c.character_maximum_length)
        for c in required
    )
    insert = (
        f"INSERT INTO {table} ({names}) VALUES ({values})"
        if required
        else f"INSERT INTO {table} DEFAULT VALUES"
    )
    await connection.execute(text(insert))
    return columns[0].column_name


async def _guarded(sessions, statement: str, table: str) -> None:
    async with sessions() as session:
        await session.execute(text("SET session_replication_role = replica"))
        with pytest.raises(DBAPIError) as refused:
            await session.execute(text(statement))
            pytest.fail(f"{table} accepted {statement}")
        # The guard's own message names the table: no other trigger or constraint answered.
        assert f"{table} keeps every record" in str(refused.value.orig)
        await session.rollback()


@pytest.mark.parametrize("table", sorted(REFUSING))
async def test_declared_guards_refuse_on_a_real_row(pg_sessions, table):
    async with pg_sessions() as session:
        await session.execute(text("SET session_replication_role = replica"))
        column = await _insert_row(session, table)
        await session.commit()
    if "UPDATE" in REFUSING[table]:
        await _guarded(pg_sessions, f"UPDATE {table} SET {column} = {column}", table)
    if "DELETE" in REFUSING[table]:
        await _guarded(pg_sessions, f"DELETE FROM {table}", table)
    if "TRUNCATE" in REFUSING[table]:
        await _guarded(pg_sessions, f"TRUNCATE {table} CASCADE", table)
    async with pg_sessions() as session:
        assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 1


async def test_every_guard_is_enable_always_in_the_migrated_catalog(pg_sessions):
    async with pg_sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.tgname, t.tgenabled::text FROM pg_trigger t "
                    "WHERE NOT t.tgisinternal AND t.tgname = ANY(:names)"
                ),
                {"names": [trigger for _, trigger, _ in PROTECTIVE_TRIGGERS]},
            )
        ).all()
    state = {name: enabled for name, enabled in rows}
    missing = [trigger for _, trigger, _ in PROTECTIVE_TRIGGERS if trigger not in state]
    assert not missing, f"declared but absent after migration: {missing}"
    weak = sorted(name for name, enabled in state.items() if enabled != "A")
    assert not weak, f"guards a replica session could bypass: {weak}"
