"""Credential migration preserves legacy rows and refuses to discard version history."""

from importlib import util

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from support import sqlite_guards

from app.core.config import get_settings
from app.models import oauth_credentials


def test_upgrade_preserves_legacy_row_and_new_versions_block_changes_and_downgrade(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'oauth.db'}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    try:
        command.upgrade(config, "20261107_0025")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oauth_tokens "
                    "(subject, encrypted_refresh_token, scopes, health_user_id) "
                    "VALUES ('me', 'synthetic-ciphertext', 'synthetic-scope', 'synthetic-account')"
                )
            )
            before = connection.execute(text("SELECT * FROM oauth_tokens")).all()
            schema = set(connection.execute(text("SELECT name, sql FROM sqlite_master")).all())
        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM oauth_tokens")).all() == before
            assert schema <= set(
                connection.execute(text("SELECT name, sql FROM sqlite_master")).all()
            )
        # Empty version history can be removed without touching legacy credentials.
        command.downgrade(config, "20261107_0025")
        assert oauth_credentials.TABLE not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oauth_credential_versions (subject, encrypted_refresh_token) "
                    "VALUES ('me', 'synthetic-new-ciphertext')"
                )
            )
        for statement in (
            "UPDATE oauth_credential_versions SET encrypted_refresh_token = 'changed'",
            "DELETE FROM oauth_credential_versions",
        ):
            with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
                connection.execute(text(statement))
        with pytest.raises(RuntimeError, match="history; downgrade refused"):
            command.downgrade(config, "20261107_0025")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM oauth_tokens")).all() == before
            assert (
                connection.scalar(
                    text("SELECT encrypted_refresh_token FROM oauth_credential_versions")
                )
                == "synthetic-new-ciphertext"
            )
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_postgres_migration_guards_match_models_and_only_touch_new_table(monkeypatch, capsys):
    spec = util.spec_from_file_location(
        "oauth_migration", "migrations/versions/20261108_0026_oauth_credentials.py"
    )
    migration = util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.POSTGRESQL_DDL == tuple(
        sql for sql in oauth_credentials.POSTGRESQL_DDL if oauth_credentials.TABLE in sql
    )
    assert migration.SQLITE_DDL == tuple(
        sql for sql in sqlite_guards.ddl(oauth_credentials) if oauth_credentials.TABLE in sql
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres:5432/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "20261107_0025:20261108_0026", sql=True)
        ddl = capsys.readouterr().out
        with pytest.raises(RuntimeError, match="online downgrade"):
            command.downgrade(Config("alembic.ini"), "20261108_0026:20261107_0025", sql=True)
    finally:
        get_settings.cache_clear()
    assert ddl.count("CREATE TABLE ") == 1
    assert "id BIGSERIAL NOT NULL" in ddl
    assert "ALTER TABLE oauth_tokens" not in ddl
    assert "DROP " not in ddl
    assert "UPDATE oauth_tokens" not in ddl
    assert ddl.count("ENABLE ALWAYS TRIGGER") == 2
