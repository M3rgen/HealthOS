"""Time dependencies add guarded evidence without rewriting or inventing archive history."""

import re
from importlib import util

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from support import sqlite_guards
from test_garmin_migration import SCHEMA, _record
from test_garmin_migration import database as database

from app.core.config import get_settings
from app.models import garmin

PREVIOUS, REVISION = "20261112_0034", "20261112_0035"
TABLES = (garmin.TIME_EVIDENCE, garmin.DAY_TIMES)


def test_time_upgrade_and_empty_downgrade_keep_existing_archive(database):
    config, engine = database
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(
            garmin.GarminRawRecord.__table__.insert().values(
                kind="heart-rate",
                key="2026-11-07",
                body={},
                body_sha256="a" * 64,
                first_seen_at=_record().first_seen_at,
            )
        )
        schema = set(connection.execute(SCHEMA).tuples())
        before = connection.execute(text("SELECT * FROM garmin_raw_records")).all()
    tables = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) - tables == set(TABLES)
    with engine.connect() as connection:
        for table in TABLES:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        # GitHub-only Apple extension: downgrading to the main line's 0034 stops at the
        # extension's earlier merge (20261112_a003), whose Apple tables stay.
        after = {row for row in connection.execute(SCHEMA).tuples() if "apple" not in row[1]}
        assert after == schema
        assert connection.execute(text("SELECT * FROM garmin_raw_records")).all() == before


@pytest.mark.parametrize("table", TABLES)
def test_time_guards_and_downgrade_refuse_evidence_loss(database, table):
    spec = util.spec_from_file_location(
        "time_evidence", "migrations/versions/20261112_0035_garmin_time_evidence.py"
    )
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, statements in (
        ("POSTGRESQL_DDL", garmin.POSTGRESQL_DDL),
        ("SQLITE_DDL", sqlite_guards.ddl(garmin)),
    ):
        assert set(getattr(module, name)) == {
            sql for sql in statements if any(name in sql for name in TABLES)
        }
    config, engine = database
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            garmin.GarminRawRecord.__table__.insert().values(
                id=1,
                kind="heart-rate",
                key="2026-11-07",
                body={},
                body_sha256="a" * 64,
                first_seen_at=_record().first_seen_at,
            )
        )
        values = {"raw_record_id": 1, "day_ordinal": 739927}
        if table == garmin.DAY_TIMES:
            values["day"] = "2026-11-07"
        connection.execute(garmin.Base.metadata.tables[table].insert().values(**values))
    for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET day_ordinal = 1"):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        assert set(TABLES) <= set(inspect(connection).get_table_names())


@pytest.mark.parametrize("target", [PREVIOUS, "20261025_0020"])
def test_time_postgresql_offline_ddl_is_additive(monkeypatch, capsys, target):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), f"{PREVIOUS}:{REVISION}", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="checked before an online downgrade"):
            command.downgrade(Config("alembic.ini"), f"{REVISION}:{target}", sql=True)
    finally:
        get_settings.cache_clear()
    assert re.findall(r"CREATE TABLE (\w+)", ddl) == list(TABLES)
    assert sorted(re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl)) == sorted(TABLES * 2)
    assert "DROP " not in ddl and "UPDATE garmin_raw_records" not in ddl
    assert all(f"INSERT INTO {table}" not in ddl for table in TABLES)
