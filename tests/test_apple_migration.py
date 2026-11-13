"""20261107_a001 only adds the Apple tables: nothing existing is altered, locked or rewritten.

Also the in-branch guard against a second Alembic head: the Apple extension's revision
follows main's 0025, so merging a newer ``main`` needs a merge revision.
"""

import re
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from support import sqlite_guards

from app.core.config import get_settings
from app.models import apple
from app.models.apple import AppleImport, ApplePayload, AppleRawRecord, AppleSource
from app.models.state import WebhookEvent

SCHEMA = text("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
PREVIOUS = "20261107_0025"
REVISION = "20261107_a001"
NOW = datetime(2026, 11, 7, tzinfo=UTC)
BOOKKEEPING = {apple.IMPORTS, apple.SOURCES}


def _payload(**values):
    return ApplePayload(
        received_at=NOW,
        channel="hae",
        headers={"automation-name": "Test"},
        body=b'{"data":{}}',
        body_sha256="1" * 64,
        body_size=11,
        **values,
    )


def _record(**values):
    return AppleRawRecord(
        channel="export",
        kind="quantity",
        type="HKQuantityTypeIdentifierStepCount",
        key="2" * 64,
        body={"value": "1"},
        body_sha256="3" * 64,
        source_name="Test Watch",
        first_seen_at=NOW,
        **values,
    )


def _import():
    return AppleImport(
        channel="export",
        origin="cli",
        status="running",
        started_at=NOW,
        bytes_read=0,
        records_read=0,
        records_new=0,
        rows_written=0,
        summary={},
    )


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'apple.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_the_revision_graph_has_exactly_one_head():
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1, (
        f"Alembic has {len(heads)} heads {sorted(heads)}: main gained a revision after "
        f"{PREVIOUS}. Add a merge revision whose down_revision is ({REVISION!r}, main's head) "
        "instead of editing an existing revision (docs/BRANCHES.md)."
    )
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_upgrade_adds_only_new_tables_and_keeps_every_existing_row(database):
    config, engine = database
    command.upgrade(config, PREVIOUS)
    with Session(engine) as session, session.begin():
        session.add(
            WebhookEvent(
                payload_hash="d" * 64,
                raw_json={"synthetic": True},
                raw_body=b' {"synthetic":true}\n',
                signature_verified_at=NOW,
            )
        )
    with engine.connect() as connection:
        schema = set(connection.execute(SCHEMA).tuples())
        rows = connection.execute(text("SELECT * FROM webhook_events")).all()
    tables = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) - tables == {*apple.TABLES, *BOOKKEEPING}
    with engine.connect() as connection:
        after = set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM webhook_events")).all() == rows
    assert schema <= after
    added = {name for _, name, _ in after - schema}
    assert set(sqlite_guards.definitions(apple.GUARDS)) <= added
    assert all("apple_" in name for name in added), added
    triggers = {name for kind, name, _ in after - schema if kind == "trigger"}
    assert triggers == set(sqlite_guards.definitions(apple.GUARDS))
    command.upgrade(config, "head")
    command.check(config)


def test_models_and_migration_define_the_same_guards():
    from importlib import util
    from pathlib import Path

    path = Path("migrations/versions/20261107_a001_apple_raw.py")
    spec = util.spec_from_file_location("apple_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == REVISION and module.down_revision == PREVIOUS
    assert module.GUARDED == apple.TABLES
    assert set(module.POSTGRESQL_DDL) == set(apple.POSTGRESQL_DDL)
    assert set(module.SQLITE_DDL) == set(sqlite_guards.ddl(apple))
    assert "app.models" not in path.read_text() and "app.services" not in path.read_text()


@pytest.mark.parametrize("table", apple.TABLES)
def test_rows_refuse_change_and_block_downgrade_until_empty(database, table):
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        if table == apple.PAYLOADS:
            session.add(_payload())
        else:
            item = _import()
            session.add(item)
            session.flush()
            session.add(_record(import_id=item.id))
    for statement in (f"UPDATE {table} SET channel = 'x'", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        # Head merges the main line (20261111_a002); SQLite runs DDL outside the transaction, so
        # the main line's own steps may already be undone here. The Apple revision never is.
        versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        assert REVISION in set(versions)


def _bookkeeping_row(session, table):
    if table == apple.IMPORTS:
        session.add(_import())
    else:
        session.add(
            AppleSource(
                source_name="Test Watch",
                first_seen_at=NOW,
                last_seen_at=NOW,
                data_types=[],
                channels=[],
                records=0,
            )
        )


@pytest.mark.parametrize("table", sorted(BOOKKEEPING))
def test_bookkeeping_rows_may_change_but_refuse_downgrade(database, table):
    # The import history and the source summary cannot be rebuilt once dropped: like every
    # other table with rows, they refuse a downgrade (app/db/downgrade_guard.py). PostgreSQL
    # rolls the whole downgrade back; SQLite runs DDL outside it, so only the row is checked.
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        _bookkeeping_row(session, table)
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE {table} SET {_CHANGE[table]}"))
    with pytest.raises(RuntimeError, match=f"{table} holds rows; downgrade refused"):
        command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        assert REVISION in set(versions)


def test_emptied_bookkeeping_and_archive_downgrade(database):
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        for table in sorted(BOOKKEEPING):
            _bookkeeping_row(session, table)
    with engine.begin() as connection:
        for table in sorted(BOOKKEEPING):
            connection.execute(text(f"DELETE FROM {table}"))
    command.downgrade(config, PREVIOUS)
    assert not ({*apple.TABLES, *BOOKKEEPING} & set(inspect(engine).get_table_names()))
    with engine.connect() as connection:
        names = {name for _, name, _ in connection.execute(SCHEMA).tuples()}
    assert not any("apple_" in name for name in names)
    command.upgrade(config, "head")
    command.check(config)


_CHANGE = {
    apple.IMPORTS: "status = 'done', records_read = 5",
    apple.SOURCES: "records = 5",
}


def test_postgresql_ddl_touches_only_its_own_tables(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), f"{PREVIOUS}:{REVISION}", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(Config("alembic.ini"), f"{REVISION}:{PREVIOUS}", sql=True)
    finally:
        get_settings.cache_clear()
    assert "LOCK TABLE" not in ddl
    assert ddl.count("CREATE TABLE ") == 4
    assert set(re.findall(r"REFERENCES (\w+)", ddl)) == {apple.PAYLOADS, apple.IMPORTS}
    assert "body JSONB NOT NULL" in ddl
    altered = set(re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl))
    assert altered == set(apple.TABLES)
    assert len(re.findall(r"ALTER TABLE", ddl)) == 4
    targets = re.findall(r"(?:CREATE (?:UNIQUE )?INDEX \w+|BEFORE [A-Z ]+?) ON (\w+)", ddl)
    assert set(targets) == {*apple.TABLES, apple.IMPORTS}
