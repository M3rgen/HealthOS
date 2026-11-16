"""Original-file schema: additive upgrade, matching guards and loss-refusing downgrade."""

import hashlib
import re
from importlib import import_module

import pytest
from alembic import command
from alembic.config import Config
from fit_factory import fit_file
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from support import sqlite_guards
from support.apple_main_line import main_line_migrations  # noqa: F401
from support.garmin_files import NOW
from test_garmin_migration import SCHEMA
from test_garmin_migration import database as database

from app.core.config import get_settings
from app.models import garmin_files, garmin_tracks
from app.models.garmin import GarminRawRecord

PREVIOUS = "20261115_0037"
REVISION = "20261115_0038"
MIGRATION = import_module("migrations.versions.20261115_0038_garmin_activity_files")

# GitHub-only Apple extension: these tests pin the main line's chain; in this tree "head" also
# joins the Apple line, whose tables a main-line downgrade keeps (support/apple_main_line.py).
pytestmark = pytest.mark.usefixtures("main_line_migrations")


def insert_evidence(connection, *, unavailable=False, outcome=None):
    raw_id = connection.scalar(
        GarminRawRecord.__table__.insert()
        .values(
            kind="activities",
            key="1",
            body={"activityId": 1},
            body_sha256="a" * 64,
            first_seen_at=NOW,
        )
        .returning(GarminRawRecord.id)
    )
    identity = {
        "activity_id": "1",
        "activity_sha256": hashlib.sha256(b"1").hexdigest(),
        "raw_record_id": raw_id,
        "fetched_at": NOW,
    }
    if unavailable or outcome is not None:
        connection.execute(
            garmin_files.GarminActivityFileResult.__table__.insert().values(
                **identity, file_id=None, **(outcome or {"status": "not_found"})
            )
        )
    else:
        content = fit_file(b"")
        connection.execute(
            garmin_files.GarminActivityFile.__table__.insert().values(
                **identity,
                body=content,
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
        )


def test_file_upgrade_preserves_every_existing_row_and_empty_downgrade(database):
    config, engine = database
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        connection.execute(
            GarminRawRecord.__table__.insert().values(
                kind="activities",
                key="1",
                body={"activityId": 1},
                body_sha256="a" * 64,
                first_seen_at=NOW,
            )
        )
        schema = set(connection.execute(SCHEMA).tuples())
        before = connection.execute(text("SELECT * FROM garmin_raw_records")).all()
    tables = set(inspect(engine).get_table_names())
    command.upgrade(config, "head")
    command.check(config)
    assert set(inspect(engine).get_table_names()) - tables == set(
        garmin_files.TABLES + garmin_tracks.TABLES
    )
    with engine.connect() as connection:
        assert schema <= set(connection.execute(SCHEMA).tuples())
        for table in garmin_files.TABLES:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert set(connection.execute(SCHEMA).tuples()) == schema
        assert connection.execute(text("SELECT * FROM garmin_raw_records")).all() == before


@pytest.mark.parametrize("unavailable", [False, True])
def test_file_guards_and_populated_downgrade_refusal(database, unavailable):
    config, engine = database
    command.upgrade(config, "head")
    with engine.begin() as connection:
        insert_evidence(connection, unavailable=unavailable)
    table = garmin_files.TABLES[int(unavailable)]
    for operation in (f"UPDATE {table} SET activity_id = 'x'", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(operation))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, PREVIOUS)
    assert set(garmin_files.TABLES) <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1


@pytest.mark.parametrize(
    "outcome, accepted",
    [
        ({"status": "invalid", "reason": "not_fit", "content_sha256": "c" * 64}, True),
        ({"status": "invalid", "reason": "too_large"}, True),
        ({"status": "invalid"}, False),
        ({"status": "not_found", "reason": "not_fit"}, False),
        ({"status": "unknown"}, False),
    ],
)
def test_invalid_results_need_a_reason_and_never_a_file(database, outcome, accepted):
    config, engine = database
    command.upgrade(config, "head")
    try:
        with engine.begin() as connection:
            insert_evidence(connection, outcome=outcome)
    except DBAPIError:
        assert not accepted
    else:
        assert accepted


def test_original_file_migration_and_models_have_identical_guards():
    assert set(MIGRATION.POSTGRESQL_DDL) == set(garmin_files.POSTGRESQL_DDL)
    assert set(MIGRATION.SQLITE_DDL) == set(sqlite_guards.ddl(garmin_files))


def test_postgres_file_ddl_is_additive_and_offline_downgrade_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), f"{PREVIOUS}:{REVISION}", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(Config("alembic.ini"), f"{REVISION}:{PREVIOUS}", sql=True)
    finally:
        get_settings.cache_clear()
    assert ddl.count("CREATE TABLE ") == 2
    assert "body BYTEA NOT NULL" in ddl and "TIMESTAMP WITH TIME ZONE" in ddl
    assert set(re.findall(r"REFERENCES (\w+)", ddl)) == {
        "garmin_raw_records",
        "garmin_activity_files",
    }
    assert set(re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl)) == set(
        garmin_files.TABLES
    )
    assert not any(
        operation in ddl for operation in ("UPDATE garmin_", "DELETE FROM ", "DROP ", "LOCK TABLE")
    )
