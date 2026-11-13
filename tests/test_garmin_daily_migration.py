"""Daily projection evidence is additive, guarded and never inferred during migration."""

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

PREVIOUS = "20261111_0031"
REVISION = "20261111_0032"
TABLE = garmin.DAILY_PROJECTIONS


def test_daily_projection_upgrade_and_empty_downgrade_preserve_old_archive(database):
    config, engine = database
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(
            garmin.GarminRawRecord.__table__.insert().values(
                kind="steps",
                key="2026-11-07",
                body={"totalSteps": 1},
                body_sha256="a" * 64,
                first_seen_at=_record().first_seen_at,
            )
        )
        schema = set(connection.execute(SCHEMA).tuples())
        before = connection.execute(text("SELECT * FROM garmin_raw_records")).all()
    tables = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) - tables == {TABLE}
    with engine.connect() as connection:
        assert schema <= set(connection.execute(SCHEMA).tuples())
        assert connection.scalar(text(f"SELECT count(*) FROM {TABLE}")) == 0
    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert set(connection.execute(SCHEMA).tuples()) == schema
        assert connection.execute(text("SELECT * FROM garmin_raw_records")).all() == before
    command.upgrade(config, "head")
    command.check(config)


def test_daily_projection_guards_match_models_and_refuse_destructive_downgrade(database):
    spec = util.spec_from_file_location(
        "daily_projections", "migrations/versions/20261111_0032_garmin_daily_projections.py"
    )
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, statements in (
        ("POSTGRESQL_DDL", garmin.POSTGRESQL_DDL),
        ("SQLITE_DDL", sqlite_guards.ddl(garmin)),
    ):
        assert set(getattr(module, name)) == {sql for sql in statements if TABLE in sql}
    config, engine = database
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            garmin.GarminDailyProjection.__table__.insert().values(
                source_record_id="steps:2026-11-07",
                raw_record_ids={"steps": 1},
                normalized_key=None,
            )
        )
    for statement in (
        f"DELETE FROM {TABLE}",
        f"UPDATE {TABLE} SET normalized_key = 'x'",
        f"UPDATE {TABLE} SET raw_record_ids = '{{}}'",
    ):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {TABLE}")) == 1


def test_daily_projection_postgresql_ddl_only_adds_table_index_and_always_guards(
    monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, f"{PREVIOUS}:{REVISION}", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(config, f"{REVISION}:{PREVIOUS}", sql=True)
    finally:
        get_settings.cache_clear()
    assert re.findall(r"CREATE TABLE (\w+)", ddl) == [TABLE]
    assert re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl) == [TABLE] * 2
    assert "DROP " not in ddl and "UPDATE garmin_raw_records" not in ddl
    assert "INSERT INTO garmin_daily_projections" not in ddl and "LOCK TABLE" not in ddl
