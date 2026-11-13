"""Account-change evidence is additive and cannot be changed or discarded."""

from importlib import util

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from support import sqlite_guards

from app.core.config import get_settings
from app.models import oauth_credentials


def test_rebind_migration_preserves_credentials_and_refuses_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'rebind.db'}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    try:
        command.upgrade(config, "20261108_0026")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oauth_credential_versions (subject, encrypted_refresh_token, "
                    "health_user_id) VALUES ('me', 'synthetic', 'old')"
                )
            )
            before = connection.execute(text("SELECT * FROM oauth_credential_versions")).all()
            schema = set(connection.execute(text("SELECT name, sql FROM sqlite_master")).all())
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT * FROM oauth_credential_versions")).all() == before
            )
            assert schema <= set(
                connection.execute(text("SELECT name, sql FROM sqlite_master")).all()
            )
        command.downgrade(config, "20261108_0026")
        assert oauth_credentials.REBIND_TABLE not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO google_account_rebind_events "
                    "(credential_version_id, old_health_user_id, new_health_user_id) "
                    "VALUES (1, 'old', 'new')"
                )
            )
        for statement in (
            "UPDATE google_account_rebind_events SET new_health_user_id = 'changed'",
            "DELETE FROM google_account_rebind_events",
        ):
            with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
                connection.execute(text(statement))
        with pytest.raises(RuntimeError, match="history; downgrade refused"):
            command.downgrade(config, "20261108_0026")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM google_account_rebind_events")) == 1
            assert (
                connection.execute(text("SELECT * FROM oauth_credential_versions")).all() == before
            )
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_postgres_rebind_ddl_matches_guards_and_offline_downgrade_refuses(monkeypatch, capsys):
    spec = util.spec_from_file_location(
        "rebind_migration", "migrations/versions/20261108_0027_google_account_rebind.py"
    )
    migration = util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.POSTGRESQL_DDL == tuple(
        sql for sql in oauth_credentials.POSTGRESQL_DDL if oauth_credentials.REBIND_TABLE in sql
    )
    assert migration.SQLITE_DDL == tuple(
        sql for sql in sqlite_guards.ddl(oauth_credentials) if oauth_credentials.REBIND_TABLE in sql
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "20261108_0026:20261108_0027", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(Config("alembic.ini"), "20261108_0027:20261108_0026", sql=True)
    finally:
        get_settings.cache_clear()
    assert ddl.count("CREATE TABLE ") == 1
    assert "DROP " not in ddl
    assert "BEFORE TRUNCATE" in ddl
    assert ddl.count("ENABLE ALWAYS TRIGGER") == 2
