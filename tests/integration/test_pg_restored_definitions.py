"""A live restore re-parses every definition: `healthos verify` must tell that from tampering."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from support.migration_chain import HEAD

from app.db.guards import definitions
from app.models.identity_constants import GUARDED_TABLES
from app.services.archive.raw_history_verification import verify_guards
from app.services.identities.analytics_views import NORMALIZED_TABLES
from app.services.identities.identity_schema import verify_identity_schema

pytestmark = pytest.mark.integration

CONSTRAINT = "ck_raw_source_decisions_known_decision"


async def schema_errors(sessions) -> list[str]:
    async with sessions() as session:
        return await verify_guards(session) + await verify_identity_schema(session, GUARDED_TABLES)


async def recreate_as_printed(sessions) -> None:
    """What a restore does with pg_dump's text: print each definition, then create it again."""
    async with sessions() as session:
        pg, _, _ = definitions()
        for trigger, (table, _) in pg.items():
            definition = await session.scalar(
                text(
                    "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c "
                    "ON c.oid = t.tgrelid WHERE t.tgname = :trigger AND c.relname = :table"
                ),
                {"trigger": trigger, "table": table},
            )
            await session.execute(text(f"DROP TRIGGER {trigger} ON {table}"))
            await (await session.connection()).exec_driver_sql(definition)
            await session.execute(text(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}"))
        for table in NORMALIZED_TABLES:
            view = f"analytics_{table}"
            definition = await session.scalar(text(f"SELECT pg_get_viewdef('{view}'::regclass)"))
            await (await session.connection()).exec_driver_sql(
                f"CREATE OR REPLACE VIEW {view} AS {definition}"
            )
        check = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name "
                "AND conrelid = 'raw_source_decisions'::regclass"
            ),
            {"name": CONSTRAINT},
        )
        await session.execute(
            text(f"ALTER TABLE raw_source_decisions DROP CONSTRAINT {CONSTRAINT}")
        )
        await (await session.connection()).exec_driver_sql(
            f"ALTER TABLE raw_source_decisions ADD CONSTRAINT {CONSTRAINT} {check}"
        )
        await session.commit()


async def test_verify_accepts_definitions_a_restore_respelled(pg_sessions) -> None:
    await recreate_as_printed(pg_sessions)
    assert await schema_errors(pg_sessions) == []
    # And twice: a backup of a restored database, restored again.
    await recreate_as_printed(pg_sessions)
    assert await schema_errors(pg_sessions) == []


async def tamper(sessions, statement: str) -> None:
    async with sessions() as session:
        for part in statement.split("; "):
            await session.execute(text(part))
        await session.commit()


@pytest.mark.parametrize(
    ("statement", "reported"),
    [
        (
            "DROP TRIGGER raw_health_data_frozen_identity ON raw_health_data; "
            "CREATE TRIGGER raw_health_data_frozen_identity BEFORE UPDATE ON raw_health_data "
            "FOR EACH ROW WHEN (false) EXECUTE FUNCTION healthos_refuse_change(); "
            "ALTER TABLE raw_health_data ENABLE ALWAYS TRIGGER raw_health_data_frozen_identity",
            "raw_health_data_frozen_identity was redefined",
        ),
        (
            "CREATE OR REPLACE VIEW analytics_energy_expenditure AS "
            "SELECT * FROM energy_expenditure",
            "analytics_energy_expenditure was redefined",
        ),
        (
            f"ALTER TABLE raw_source_decisions DROP CONSTRAINT {CONSTRAINT}; "
            f"ALTER TABLE raw_source_decisions ADD CONSTRAINT {CONSTRAINT} "
            "CHECK ((decision)::text = ANY (ARRAY['link'::text, 'reject'::text]))",
            f"{CONSTRAINT} was changed",
        ),
    ],
    ids=["trigger", "view", "check"],
)
async def test_real_tampering_after_a_restore_is_still_reported(
    pg_sessions, statement: str, reported: str
) -> None:
    await recreate_as_printed(pg_sessions)
    await tamper(pg_sessions, statement)
    errors = await schema_errors(pg_sessions)
    assert any(reported in error for error in errors), errors


def test_v0_2_2_apple_revision_upgrades_keeping_its_rows(database_url, alembic_config) -> None:
    """A database at v0.2.2's 20261107_a001 upgrades to head with every Apple row in place."""
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    apple_ddl = """
    CREATE TABLE apple_payloads (id bigserial PRIMARY KEY, body bytea NOT NULL);
    CREATE TABLE apple_raw_records (id bigserial PRIMARY KEY, body jsonb NOT NULL);
    CREATE TRIGGER apple_payloads_append_only BEFORE UPDATE OR DELETE ON apple_payloads
      FOR EACH ROW EXECUTE FUNCTION healthos_refuse_change();
    CREATE TRIGGER apple_raw_records_append_only BEFORE UPDATE OR DELETE ON apple_raw_records
      FOR EACH ROW EXECUTE FUNCTION healthos_refuse_change();
    ALTER TABLE apple_payloads ENABLE ALWAYS TRIGGER apple_payloads_append_only;
    ALTER TABLE apple_raw_records ENABLE ALWAYS TRIGGER apple_raw_records_append_only;
    """
    command.upgrade(alembic_config, "20261109_p001")
    try:
        with engine.begin() as connection:
            # Reproduce the stamp written by both v0.2.2 and the retired placeholder.
            connection.execute(text("UPDATE alembic_version SET version_num = '20261107_a001'"))
            connection.exec_driver_sql(apple_ddl)
            connection.execute(
                text("INSERT INTO apple_payloads (body) VALUES (:body)"), {"body": b"synthetic"}
            )
            connection.execute(text("INSERT INTO apple_raw_records (body) VALUES ('{\"k\": 1}')"))
        command.upgrade(alembic_config, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).all() == [
                (HEAD,)
            ]
            assert connection.scalar(text("SELECT count(*) FROM apple_payloads")) == 1
            assert bytes(connection.scalar(text("SELECT body FROM apple_payloads"))) == b"synthetic"
            assert connection.scalar(text("SELECT count(*) FROM apple_raw_records")) == 1
            triggers = connection.scalars(
                text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname LIKE 'apple_%' AND NOT tgisinternal"
                )
            ).all()
        assert sorted(triggers) == ["apple_payloads_append_only", "apple_raw_records_append_only"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", ["apple_payloads", "apple_raw_records", "elsewhere"])
async def test_verify_reports_unregistered_guards_regardless_of_table_name(
    pg_sessions, table
) -> None:
    if any(relation == table for relation, _, _ in definitions()[2]):
        pytest.skip("registered here: the GitHub-only Apple extension guards its own tables")
    async with pg_sessions() as session:
        await session.execute(text(f"CREATE TABLE {table} (id bigserial PRIMARY KEY)"))
        await session.execute(
            text(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON "
                f"{table} FOR EACH STATEMENT EXECUTE FUNCTION healthos_refuse_change()"
            )
        )
        await session.commit()
    async with pg_sessions() as session:
        assert await verify_guards(session) == [
            f"protective trigger {table}_no_truncate sits on {table}, "
            "which is not a guarded table (was a guarded table renamed or replaced?)"
        ]
        await session.rollback()
