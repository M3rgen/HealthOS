"""Real PostgreSQL migration rollback, writer serialization and restore guard coverage."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from support.apple_main_line import main_line_migrations  # noqa: F401
from support.migration_chain import HEAD
from support.plans import sample

from app.models.plan import PlanState, PlanVersion
from app.schemas.plan import PlanStateBody
from app.services.operations.plan_validation import InvalidPlan
from app.services.operations.plans import get_plan, import_document, lock_plan, put_state

pytestmark = pytest.mark.integration
PREVIOUS = "20261114_0036"


# GitHub-only Apple extension: the main line's chain (support/apple_main_line.py); in this tree
# "head" also joins the Apple line, whose tables a main-line downgrade keeps.
@pytest.mark.usefixtures("main_line_migrations")
def test_plan_migration_forward_backward_and_no_drift(database_url, alembic_config):
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        command.upgrade(alembic_config, PREVIOUS)
        before = set(inspect(engine).get_table_names())
        # The plan revision itself adds exactly its two tables; later revisions add their own.
        command.upgrade(alembic_config, "20261115_0037")
        assert set(inspect(engine).get_table_names()) - before == {"plan_state", "plan_versions"}
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        command.downgrade(alembic_config, PREVIOUS)
        assert set(inspect(engine).get_table_names()) == before
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", ["plan_versions", "plan_state"])
def test_populated_plan_tables_refuse_and_roll_back_whole_downgrade(database_url, migrated, table):
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        with Session(engine) as session, session.begin():
            if table == "plan_versions":
                session.add(
                    PlanVersion(content=sample(), content_sha256="a" * 64, schema_version=1)
                )
            else:
                session.add(PlanState(id=1, schema_version=1, checked_item_ids=[]))
        before = set(inspect(engine).get_table_names())
        for destination in (PREVIOUS, "base"):
            with pytest.raises(RuntimeError, match=f"{table} holds rows; downgrade refused"):
                command.downgrade(migrated, destination)
            assert set(inspect(engine).get_table_names()) == before
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
                assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        command.check(migrated)
    finally:
        engine.dispose()


async def test_concurrent_duplicate_imports_create_exactly_one_version(pg_sessions):
    async def importing():
        async with pg_sessions.begin() as session:
            row, added = await import_document(session, sample())
        return row.id, added

    results = await asyncio.gather(*(importing() for _ in range(4)))
    assert len({version for version, _ in results}) == 1
    assert sum(added for _, added in results) == 1
    async with pg_sessions() as session:
        assert len((await session.scalars(select(PlanVersion))).all()) == 1
        assert (await get_plan(session))["plan"] == sample()


async def test_state_waits_for_import_then_validates_new_active_ids(pg_sessions):
    async with pg_sessions.begin() as session:
        await import_document(session, sample())
        await put_state(session, PlanStateBody(start_date=None, checked_item_ids=["example-grain"]))
    started = asyncio.Event()

    async def writer():
        async with pg_sessions.begin() as session:
            started.set()
            with pytest.raises(InvalidPlan, match="unknown_identifier"):
                await put_state(
                    session, PlanStateBody(start_date=None, checked_item_ids=["example-grain"])
                )

    async with pg_sessions.begin() as importer:
        await lock_plan(importer)
        await import_document(importer, {"schema": 1})
        task = asyncio.create_task(writer())
        await started.wait()
    await task
    async with pg_sessions() as session:
        assert (await session.get(PlanState, 1)).checked_item_ids == ["example-grain"]
        assert (await get_plan(session))["state"]["checked_item_ids"] == []
