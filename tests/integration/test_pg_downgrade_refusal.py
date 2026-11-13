"""A downgrade that would lose rows refuses, on PostgreSQL, wherever it starts."""

from argparse import Namespace
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from support.migration_chain import HEAD

from app.db import downgrade_guard

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSERTS = {
    "raw_health_data": (
        "INSERT INTO raw_health_data (id, data_type, google_data_point_id, raw_json, "
        "payload_hash, first_seen_at, last_seen_at, created_at, updated_at, is_quarantined) "
        "VALUES (gen_random_uuid(), 'steps', 'synthetic:1', CAST(:raw AS jsonb), 'h', :t, :t, "
        ":t, :t, false)"
    ),
    "manual_measurements": (
        "INSERT INTO manual_measurements (id, metric, value, measured_at, created_at, "
        "updated_at) VALUES (gen_random_uuid(), 'weight', 70, :t, :t, :t)"
    ),
    "manual_workouts": (
        "INSERT INTO manual_workouts (id, exercise_type, start_time, end_time, created_at, "
        "updated_at) VALUES (gen_random_uuid(), 'walk', :t, :t, :t, :t)"
    ),
}


def _sync(database_url):
    return create_engine(database_url.replace("+asyncpg", "+psycopg"))


def _version(engine):
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _exists(engine, table):
    with engine.connect() as connection:
        return connection.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is not None


@pytest.mark.parametrize("table", sorted(INSERTS))
def test_a_table_with_rows_refuses_downgrade_base_and_changes_nothing(
    database_url, migrated, table
):
    engine = _sync(database_url)
    with engine.begin() as connection:
        connection.execute(text(INSERTS[table]), {"t": NOW, "raw": '{"a": 1}'})

    with pytest.raises(RuntimeError, match=f"{table} holds rows.*downgrade refused"):
        command.downgrade(migrated, "base")

    # Refused before the first step: no revision was undone, not even above the commit in 0007.
    assert _version(engine) == HEAD
    assert _exists(engine, table)
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    engine.dispose()


def test_rows_refuse_a_downgrade_that_starts_below_the_commit_point(database_url, migrated):
    engine = _sync(database_url)
    command.downgrade(migrated, "20260930_0005")
    with engine.begin() as connection:
        connection.execute(text(INSERTS["manual_measurements"]), {"t": NOW})

    with pytest.raises(RuntimeError, match="manual_measurements holds rows"):
        command.downgrade(migrated, "base")

    assert _version(engine) == "20260930_0005"
    assert _exists(engine, "manual_measurements")
    engine.dispose()


def test_an_evidence_column_refuses_but_an_empty_one_does_not(database_url, migrated):
    engine = _sync(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO webhook_events (payload_hash, raw_json, raw_body, "
                "signature_verified_at, element_count, failure_count, created_at, updated_at) "
                "VALUES ('h', CAST('{}' AS jsonb), :body, :t, 1, 0, :t, :t)"
            ),
            {"t": NOW, "body": b"signed bytes"},
        )

    with pytest.raises(RuntimeError, match="webhook_events holds values.*downgrade refused"):
        command.downgrade(migrated, "20260818_0002")

    assert _version(engine) == HEAD
    engine.dispose()


def test_an_empty_database_downgrades_to_base_and_upgrades_again(database_url, migrated):
    engine = _sync(database_url)
    command.downgrade(migrated, "base")
    assert not _exists(engine, "raw_health_data")
    command.upgrade(migrated, "head")
    assert _version(engine) == HEAD
    engine.dispose()


def test_no_consent_flag_reaches_the_raw_archive(database_url, migrated):
    engine = _sync(database_url)
    with engine.begin() as connection:
        connection.execute(text(INSERTS["raw_health_data"]), {"t": NOW, "raw": '{"a": 1}'})
    migrated.cmd_opts = Namespace(
        x=["allow-version-loss=1", "allow-identity-loss=1", "allow-flag-loss=1"]
    )

    with pytest.raises(RuntimeError, match="raw_health_data holds rows"):
        command.downgrade(migrated, "base")

    assert _version(engine) == HEAD
    engine.dispose()


def test_a_stamp_is_not_a_downgrade(database_url, migrated):
    engine = _sync(database_url)
    with engine.begin() as connection:
        connection.execute(text(INSERTS["manual_measurements"]), {"t": NOW})
    command.stamp(migrated, "20260818_0001", purge=True)
    assert _version(engine) == "20260818_0001"
    command.stamp(migrated, "head", purge=True)
    assert _version(engine) == HEAD
    engine.dispose()


def test_rebuildable_derived_days_do_not_block_a_downgrade(database_url, migrated):
    engine = _sync(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO derived_days (day, timezone, method, window_start, window_stop, "
                "civil_start, civil_stop) VALUES ('2026-01-01', 'UTC', 'daily', :t, :t, :t, :t)"
            ),
            {"t": NOW},
        )
    command.downgrade(migrated, "20261025_0017")
    assert not _exists(engine, "derived_days")
    engine.dispose()


def test_every_table_dropped_below_the_commit_point_is_checked_first(
    database_url, migrated, monkeypatch
):
    # Every revision below 0007 drops only what the preflight names: a new drop there
    # has to be added to BELOW_AUTOCOMMIT or this fails.
    dropped: list[str] = []
    real = downgrade_guard._refuse_while_held

    def spy(operations, table, schema, where, what):
        dropped.append(table)
        real(operations, table, schema, where, what)

    monkeypatch.setattr(downgrade_guard, "_refuse_while_held", spy)
    command.downgrade(migrated, "20261017_0007")
    dropped.clear()
    command.downgrade(migrated, "base")

    planned = {table for losses in downgrade_guard.BELOW_AUTOCOMMIT.values() for table, _ in losses}
    assert set(dropped) <= planned, set(dropped) - planned
    assert {"raw_health_data", "manual_measurements", "webhook_events"} <= planned
