"""20261107_0024 only adds the Garmin tables: nothing existing is altered, locked or rewritten."""

import re
from datetime import UTC, date, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from support import sqlite_guards

from app.core.config import get_settings
from app.models import garmin
from app.models.garmin import GarminApiResponse, GarminPullState, GarminRawRecord
from app.models.state import WebhookEvent

SCHEMA = text("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
PREVIOUS = "20261102_0023"
REVISION = "20261107_0024"
ORIGINAL_TABLES = (garmin.RESPONSES, garmin.RECORDS)
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
    assert set(inspect(engine).get_table_names()) - tables == {*ORIGINAL_TABLES, garmin.STATE}
    with engine.connect() as connection:
        after = set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM webhook_events")).all() == rows
    assert schema <= after
    added = {name for _, name, _ in after - schema}
    assert {
        name
        for name, (table, _) in sqlite_guards.definitions(garmin.GUARDS).items()
        if table in ORIGINAL_TABLES
    } <= added
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
    assert module.GUARDED == ORIGINAL_TABLES
    assert set(module.POSTGRESQL_DDL) == {
        sql
        for sql in garmin.POSTGRESQL_DDL
        if not any(table in sql for table in garmin.TABLES if table not in ORIGINAL_TABLES)
    }
    assert set(module.SQLITE_DDL) == {
        sql
        for sql in sqlite_guards.ddl(garmin)
        if not any(table in sql for table in garmin.TABLES if table not in ORIGINAL_TABLES)
    }


@pytest.mark.parametrize("table", ORIGINAL_TABLES)
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
    assert not ({*ORIGINAL_TABLES, garmin.STATE} & set(inspect(engine).get_table_names()))
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
    assert altered == set(ORIGINAL_TABLES)
    assert len(re.findall(r"ALTER TABLE", ddl)) == 4
    targets = re.findall(r"(?:CREATE (?:UNIQUE )?INDEX \w+|BEFORE [A-Z ]+?) ON (\w+)", ddl)
    assert set(targets) == set(ORIGINAL_TABLES)


OBSERVATION_REVISION = "20261111_0030"
OBSERVATION_PREVIOUS = "20261109_0029"


def test_observation_upgrade_preserves_archive_bytes_and_infers_no_history(database):
    config, engine = database
    command.upgrade(config, OBSERVATION_PREVIOUS)
    with Session(engine) as session, session.begin():
        response = _response()
        session.add(response)
        session.flush()
        session.add(_record(response_id=response.id))
    with engine.connect() as connection:
        schema = set(connection.execute(SCHEMA).tuples())
        before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in ORIGINAL_TABLES
        }
    command.upgrade(config, OBSERVATION_REVISION)
    with engine.connect() as connection:
        assert schema <= set(connection.execute(SCHEMA).tuples())
        assert connection.scalar(text(f"SELECT count(*) FROM {garmin.OBSERVATIONS}")) == 0
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows
    foreign_keys = inspect(engine).get_foreign_keys(garmin.OBSERVATIONS)
    assert {fk["referred_table"] for fk in foreign_keys} == set(ORIGINAL_TABLES)
    command.upgrade(config, "head")
    command.check(config)
    # An empty observation table can be removed without touching earlier evidence.
    command.downgrade(config, OBSERVATION_PREVIOUS)
    with engine.connect() as connection:
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows


def test_observation_guards_match_frozen_migration_and_refuse_changes(database):
    from importlib import util

    from app.models.garmin import GarminRecordObservation

    spec = util.spec_from_file_location(
        "garmin_observations", "migrations/versions/20261111_0030_garmin_observations.py"
    )
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.POSTGRESQL_DDL) == {
        sql for sql in garmin.POSTGRESQL_DDL if garmin.OBSERVATIONS in sql
    }
    assert set(module.SQLITE_DDL) == {
        sql for sql in sqlite_guards.ddl(garmin) if garmin.OBSERVATIONS in sql
    }
    config, engine = database
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        response = _response()
        session.add(response)
        session.flush()
        record = _record(response_id=response.id)
        session.add(record)
        session.flush()
        session.add(
            GarminRecordObservation(
                raw_record_id=record.id, response_id=response.id, observed_at=NOW
            )
        )
    for statement in (
        f"UPDATE {garmin.OBSERVATIONS} SET observed_at = '2026-11-08'",
        f"UPDATE {garmin.OBSERVATIONS} SET raw_record_id = 999",
        f"UPDATE {garmin.OBSERVATIONS} SET response_id = 999",
        f"DELETE FROM {garmin.OBSERVATIONS}",
    ):
        with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, OBSERVATION_PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {garmin.OBSERVATIONS}")) == 1


def test_observation_postgresql_upgrade_is_additive_and_all_guards_are_always(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, f"{OBSERVATION_PREVIOUS}:{OBSERVATION_REVISION}", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(config, f"{OBSERVATION_REVISION}:{OBSERVATION_PREVIOUS}", sql=True)
    finally:
        get_settings.cache_clear()
    assert ddl.count("CREATE TABLE ") == 1
    assert set(re.findall(r"REFERENCES (\w+)", ddl)) == set(ORIGINAL_TABLES)
    assert "INSERT INTO garmin_record_observations" not in ddl
    assert "UPDATE garmin_raw_records" not in ddl and "UPDATE garmin_api_responses" not in ddl
    assert re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl) == [garmin.OBSERVATIONS] * 2
    assert "BEFORE UPDATE OR DELETE" in ddl and "BEFORE TRUNCATE" in ddl
    assert "observed_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl


HEART_REVISION = "20261111_0031"
HEART_TABLES = (garmin.HEART_SETS, garmin.HEART_MEMBERS)


def test_heart_set_migration_is_additive_guarded_and_infers_no_membership(database):
    from importlib import util

    from app.models.garmin import GarminHeartMember, GarminHeartSet

    config, engine = database
    command.upgrade(config, OBSERVATION_REVISION)
    with Session(engine) as session, session.begin():
        record = _record()
        session.add(record)
        session.flush()
        identifier = record.id
    with engine.connect() as connection:
        before = set(connection.execute(SCHEMA).tuples())
        records = connection.execute(text("SELECT * FROM garmin_raw_records")).all()
    command.upgrade(config, HEART_REVISION)
    with engine.connect() as connection:
        assert before <= set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM garmin_raw_records")).all() == records
        for table in HEART_TABLES:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    command.upgrade(config, "head")
    command.check(config)
    spec = util.spec_from_file_location(
        "heart_sets", "migrations/versions/20261111_0031_garmin_heart_sets.py"
    )
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, model_ddl in (
        ("POSTGRESQL_DDL", garmin.POSTGRESQL_DDL),
        ("SQLITE_DDL", sqlite_guards.ddl(garmin)),
    ):
        assert set(getattr(module, name)) == {
            sql for sql in model_ddl if any(table in sql for table in HEART_TABLES)
        }
    with Session(engine) as session, session.begin():
        current = GarminHeartSet(
            source_record_id="heart-rate:2026-11-07",
            raw_record_id=identifier,
            members_sha256="0" * 64,
        )
        session.add(current)
        session.flush()
        session.add(GarminHeartMember(set_id=current.id, sample_key="synthetic:v2"))
    for table, column in (
        (garmin.HEART_SETS, "members_sha256"),
        (garmin.HEART_MEMBERS, "sample_key"),
    ):
        for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET {column} = 'x'"):
            with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
                connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, OBSERVATION_REVISION)
    with engine.connect() as connection:
        for table in HEART_TABLES:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1


def test_heart_sets_postgresql_ddl_adds_only_tables_and_always_guards(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, f"{OBSERVATION_REVISION}:{HEART_REVISION}", sql=True)
        ddl = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert ddl.count("CREATE TABLE ") == 2
    assert set(re.findall(r"REFERENCES (\w+)", ddl)) == {garmin.RECORDS, garmin.HEART_SETS}
    assert set(re.findall(r"ALTER TABLE (\w+) ENABLE ALWAYS TRIGGER", ddl)) == set(HEART_TABLES)
    assert "INSERT INTO garmin_heart" not in ddl and "UPDATE garmin_raw_records" not in ddl
    assert "DROP " not in ddl and "LOCK TABLE" not in ddl


def test_normalization_state_upgrade_preserves_archive_and_pull_checkpoints(database):
    config, engine = database
    command.upgrade(config, "20261111_0033")
    with Session(engine) as session, session.begin():
        session.add(_record())
        session.add(GarminPullState(kind="steps", backfill_next=date(2026, 1, 1)))
    with engine.connect() as connection:
        before = set(connection.execute(SCHEMA).tuples())
        raw = connection.execute(text("SELECT * FROM garmin_raw_records")).all()
        states = connection.execute(text("SELECT * FROM garmin_pull_state")).all()
    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        assert before <= set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM garmin_raw_records")).all() == raw
        assert connection.execute(text("SELECT * FROM garmin_pull_state")).all() == states
        assert connection.scalar(text("SELECT count(*) FROM garmin_normalization_state")) == 0
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO garmin_normalization_state (id) VALUES (1)"))
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(
            text("UPDATE garmin_normalization_state SET last_error_code = 'arbitrary-text'")
        )


def test_normalization_state_postgresql_upgrade_only_adds_safe_bookkeeping(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "20261111_0033:20261112_0034", sql=True)
        ddl = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert ddl.count("CREATE TABLE ") == 1
    assert "CREATE TABLE garmin_normalization_state" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl and "safe_error_code" in ddl
    assert "garmin_normalization_failed" in ddl
    assert "DROP " not in ddl and "LOCK TABLE" not in ddl and "DELETE FROM" not in ddl
    assert "UPDATE garmin_" not in ddl and "INSERT INTO garmin_" not in ddl
