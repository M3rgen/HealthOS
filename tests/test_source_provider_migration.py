"""20261107_0025 adds the provider columns to the normalized tables and nothing else: no table
is rebuilt, no trigger or view is lost, every existing row reads ``GOOGLE``."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from importlib import util

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.providers import GARMIN, GOOGLE, garmin_record_key
from app.models.health import ExerciseSession, Steps
from app.models.state import RawHealthData

PREVIOUS = "20261107_0024"
REVISION = "20261107_0025"
NOW = datetime(2026, 11, 7, tzinfo=UTC)
OBJECTS = text(
    "SELECT type, name, tbl_name FROM sqlite_master WHERE type IN ('trigger', 'view', 'index') "
    "AND name NOT LIKE 'sqlite_%' ORDER BY 1, 2"
)


def _migration():
    spec = util.spec_from_file_location(
        "migration_0025", f"migrations/versions/{REVISION}_source_provider.py"
    )
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'provider.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_migration_lists_every_normalized_table():
    from app.models.eligibility import NORMALIZED_TABLES

    assert set(_migration().TABLES) == set(NORMALIZED_TABLES)


def test_upgrade_adds_two_columns_per_table_and_keeps_rows_triggers_and_views(database):
    config, engine = database
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO steps (id, google_data_point_id, start_time, end_time, count, "
                "is_quarantined, created_at, updated_at) VALUES "
                "('11111111111111111111111111111111', 'synthetic:one', '2026-11-06 00:00:00', "
                "'2026-11-07 00:00:00', 1234, 0, '2026-11-07 00:00:00', '2026-11-07 00:00:00')"
            )
        )
        objects = set(connection.execute(OBJECTS).tuples())
        columns = {
            table: {column["name"] for column in inspect(connection).get_columns(table)}
            for table in _migration().TABLES
        }
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert set(connection.execute(OBJECTS).tuples()) == objects  # nothing rebuilt or lost
        for table in _migration().TABLES:
            names = {column["name"] for column in inspect(connection).get_columns(table)}
            assert names - columns[table] == {"provider", "source_record_id"}, table
        row = connection.execute(
            text("SELECT google_data_point_id, count, provider, source_record_id FROM steps")
        ).one()
    assert row == ("synthetic:one", 1234, GOOGLE, None)  # backfilled by the default, unchanged
    # The metadata matches the schema at head (later revisions only add their own tables).
    command.upgrade(config, "head")
    command.check(config)


def test_downgrade_drops_the_columns_but_refuses_while_a_garmin_row_exists(database):
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        session.add(
            Steps(
                google_data_point_id="synthetic:google",
                start_time=NOW,
                end_time=NOW,
                count=Decimal(1),
            )
        )
    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        names = {column["name"] for column in inspect(connection).get_columns("steps")}
        assert "provider" not in names and "source_record_id" not in names
        assert connection.scalar(text("SELECT count(*) FROM steps")) == 1
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        session.add(
            ExerciseSession(
                google_data_point_id=garmin_record_key("activities", "1001"),
                provider=GARMIN,
                source_record_id="activities:1001",
                start_time=NOW,
                end_time=NOW,
            )
        )
    with pytest.raises(RuntimeError, match="non-Google row"):
        command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM exercise_sessions")) == 1
        assert "provider" in {c["name"] for c in inspect(connection).get_columns("steps")}


def test_postgresql_ddl_only_adds_columns(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), f"{PREVIOUS}:{REVISION}", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(Config("alembic.ini"), f"{REVISION}:{PREVIOUS}", sql=True)
    finally:
        get_settings.cache_clear()
    statements = re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+) ([^;]+);", ddl)
    assert len(statements) == 2 * len(_migration().TABLES)
    assert {table for table, _, _ in statements} == set(_migration().TABLES)
    for _, column, rest in statements:
        if column == "provider":
            assert "VARCHAR(16) DEFAULT 'GOOGLE' NOT NULL" in rest
        else:
            assert column == "source_record_id" and "VARCHAR(512)" in rest
    for forbidden in ("LOCK TABLE", "CREATE INDEX", "CREATE TABLE", "DROP", "UPDATE "):
        assert forbidden not in ddl.replace("UPDATE alembic_version", ""), forbidden


def test_raw_health_data_is_untouched():
    """Garmin never enters ``raw_health_data`` (rule 3: it holds what Google sent); the
    provider lives on the normalized tables only."""
    assert "provider" not in {column.name for column in RawHealthData.__table__.columns}
    source = open(f"migrations/versions/{REVISION}_source_provider.py", encoding="utf-8").read()
    assert "raw_health_data" not in source.split("def upgrade")[1].split("def downgrade")[0]
