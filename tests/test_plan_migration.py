"""Additive plan DDL, frozen guards and downgrade protection for both new tables."""

from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from support import sqlite_guards
from support.apple_main_line import main_line_migrations  # noqa: F401
from support.plans import sample

from app.core.config import get_settings
from app.db.downgrade_guard import DISPOSABLE_TABLES
from app.models import plan

MIGRATION = import_module("migrations.versions.20261115_0037_plan")
TABLES = {"plan_versions", "plan_state"}

# GitHub-only Apple extension: these tests pin the main line's chain; in this tree "head" also
# joins the Apple line, whose tables a main-line downgrade keeps (support/apple_main_line.py).
pytestmark = pytest.mark.usefixtures("main_line_migrations")


def test_frozen_plan_guards_are_registered_and_not_disposable():
    assert MIGRATION.POSTGRESQL_DDL == plan.GUARDS.postgresql_ddl
    assert MIGRATION.SQLITE_DDL == sqlite_guards.ddl(plan)
    assert not TABLES & DISPOSABLE_TABLES
    assert MIGRATION.down_revision == "20261114_0036"
    source = Path(MIGRATION.__file__).read_text()
    assert "from app" not in source
    assert "drop_table" not in source.split("def downgrade")[0]


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'plan.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield engine, Config("alembic.ini")
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_empty_migration_roundtrip_and_alembic_check(database):
    engine, config = database
    command.upgrade(config, MIGRATION.down_revision)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, MIGRATION.revision)
    assert set(inspect(engine).get_table_names()) - before == TABLES
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, MIGRATION.down_revision)
    assert set(inspect(engine).get_table_names()) == before
    command.upgrade(config, "head")
    command.check(config)
    with pytest.raises(RuntimeError, match="online downgrade"):
        command.downgrade(config, f"{MIGRATION.revision}:{MIGRATION.down_revision}", sql=True)


@pytest.mark.parametrize("table", sorted(TABLES))
def test_populated_new_tables_refuse_downgrade_without_changes(database, table):
    engine, config = database
    command.upgrade(config, MIGRATION.revision)
    with Session(engine) as session, session.begin():
        if table == "plan_versions":
            session.add(
                plan.PlanVersion(content=sample(), content_sha256="a" * 64, schema_version=1)
            )
        else:
            session.add(plan.PlanState(id=1, schema_version=1, checked_item_ids=[]))
    before = set(inspect(engine).get_table_names())
    with pytest.raises(RuntimeError, match=f"{table} holds rows; downgrade refused"):
        command.downgrade(config, MIGRATION.down_revision)
    assert set(inspect(engine).get_table_names()) == before
    with engine.connect() as connection:
        model = plan.PlanVersion if table == "plan_versions" else plan.PlanState
        assert len(connection.execute(select(model)).all()) == 1
    command.upgrade(config, "head")
    command.check(config)
