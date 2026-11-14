"""Migration 0036 never queues every archive writer behind an open sync transaction."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from support.migration_chain import HEAD

pytestmark = pytest.mark.integration


def test_0036_gives_up_on_a_held_writer_lock_and_changes_nothing(
    database_url, alembic_config, monkeypatch
) -> None:
    command.upgrade(alembic_config, "20261112_0035")
    # Safety net for the test only: without the migration's own lock_timeout the upgrade
    # would wait as long as the writer stays open; this turns that into a failure.
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=20000")
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    blocker = engine.connect()
    try:
        # What an uncommitted sync page holds on the raw archive.
        blocker.execute(text("LOCK TABLE raw_health_data IN ROW EXCLUSIVE MODE"))
        with pytest.raises(OperationalError, match="lock timeout"):
            command.upgrade(alembic_config, "head")
        blocker.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20261112_0035"
            )
            assert connection.scalar(text("SELECT to_regclass('google_heart_hours')")) is None
            assert not connection.scalar(
                text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'heart\\_%'")
            )
        monkeypatch.delenv("PGOPTIONS")
        command.upgrade(alembic_config, "head")  # once the writer commits, it just works
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        # The downgrade sets the same limit and removes only the derived state.
        blocker.execute(text("LOCK TABLE heart_rate IN ROW EXCLUSIVE MODE"))
        with pytest.raises(OperationalError, match="lock timeout"):
            command.downgrade(alembic_config, "20261112_0035")
        blocker.rollback()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    finally:
        blocker.close()
        engine.dispose()
