"""20261107_0024 only adds the Garmin tables: nothing existing is altered, locked or rewritten."""

import re
from datetime import UTC, date, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import garmin
from app.models.garmin import GarminApiResponse, GarminPullState, GarminRawRecord
from app.models.state import WebhookEvent

SCHEMA = text("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
PREVIOUS = "20261102_0023"
REVISION = "20261107_0024"
NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _response(**values):
    return GarminApiResponse(
        fetched_at=NOW,
        kind="steps",
        method="GET",
        path="/usersummary-service/stats/steps/daily/2026-11-01/2026-11-07",
        request_sha256="0" * 64,
        attempt=0,
        status_code=200,
        headers={},
        body=b"[]",
        body_sha256="1" * 64,
        body_size=2,
        complete=True,
        **values,
    )


def _record(**values):
    return GarminRawRecord(
        kind="steps",
        key="2026-11-07",
        body={"calendarDate": "2026-11-07", "totalSteps": 1},
        body_sha256="2" * 64,
        first_seen_at=NOW,
        **values,
    )


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'garmin.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


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
    assert set(inspect(engine).get_table_names()) - tables == {*garmin.TABLES, garmin.STATE}
    with engine.connect() as connection:
        after = set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM webhook_events")).all() == rows
    assert schema <= after
    added = {name for _, name, _ in after - schema}
    assert set(garmin.SQLITE_TRIGGER_SQL) <= added
    assert all(name.startswith("garmin_") or "garmin_" in name for name in added), added
    # The metadata matches the schema at head (0025 adds the provider columns after this).
    command.upgrade(config, "head")
    command.check(config)


def test_models_and_migration_define_the_same_guards():
    from importlib import util

    spec = util.spec_from_file_location(
        "garmin_migration", "migrations/versions/20261107_0024_garmin_raw.py"
    )
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.GUARDED == garmin.TABLES
    assert set(module.POSTGRESQL_DDL) == set(garmin.POSTGRESQL_DDL)
    assert set(module.SQLITE_DDL) == set(garmin.SQLITE_DDL)


@pytest.mark.parametrize("table", garmin.TABLES)
def test_rows_refuse_change_and_block_downgrade_until_empty(database, table):
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        response = _response()
        session.add(response)
        session.flush()
        if table == garmin.RECORDS:
            session.add(_record(response_id=response.id))
    for statement in (f"UPDATE {table} SET kind = 'x'", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) >= 1


def test_state_rows_may_change_and_empty_archive_downgrades(database):
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        session.add(GarminPullState(kind="steps", backfill_since=date(2026, 1, 1)))
    with engine.begin() as connection:
        connection.execute(text("UPDATE garmin_pull_state SET backfill_next = '2026-02-01'"))
    command.downgrade(config, PREVIOUS)
    assert not ({*garmin.TABLES, garmin.STATE} & set(inspect(engine).get_table_names()))
    with engine.connect() as connection:
        names = {name for _, name, _ in connection.execute(SCHEMA).tuples()}
    assert not any("garmin_" in name for name in names)
    command.upgrade(config, "head")
    command.check(config)


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
    assert ddl.count("CREATE TABLE ") == 3
    assert re.findall(r"REFERENCES (\w+)", ddl) == ["garmin_api_responses"]
    assert "body JSONB NOT NULL" in ddl
    altered = set(re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl))
    assert altered == set(garmin.TABLES)
    assert len(re.findall(r"ALTER TABLE", ddl)) == 4
    targets = re.findall(r"(?:CREATE (?:UNIQUE )?INDEX \w+|BEFORE [A-Z ]+?) ON (\w+)", ddl)
    assert set(targets) == set(garmin.TABLES)
