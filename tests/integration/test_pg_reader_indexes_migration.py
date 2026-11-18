"""20261116_0041 builds its partial indexes concurrently and never keeps an invalid one.

A ``CREATE INDEX CONCURRENTLY`` that stops half way leaves an invalid index under the name.
Here a failed unique build leaves exactly that behind (two non-Google rows share a provider);
the upgrade drops it and builds the real one. Synthetic rows only.
"""

from __future__ import annotations

from importlib import import_module

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration
MIGRATION = import_module("migrations.versions.20261116_0041_reader_indexes")
NAMES = [index[0] for index in MIGRATION.INDEXES]
STATE = (
    "SELECT c.relname, i.indisvalid, i.indisunique, pg_get_indexdef(i.indexrelid) "
    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:names)"
)


def _engine(database_url):
    return create_engine(database_url.replace("+asyncpg", "+psycopg"), isolation_level="AUTOCOMMIT")


def _state(engine) -> dict:
    with engine.connect() as connection:
        rows = connection.execute(text(STATE), {"names": NAMES}).all()
    return {name: (valid, unique, definition) for name, valid, unique, definition in rows}


def test_upgrade_replaces_an_invalid_leftover_and_builds_every_index(database_url, alembic_config):
    command.upgrade(alembic_config, MIGRATION.down_revision)
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "INSERT INTO heart_rate (id, google_data_point_id, provider, timestamp, bpm) "
                    "SELECT gen_random_uuid(), 'garmin:synthetic:' || n, 'GARMIN', "
                    "timestamptz '2026-01-01T00:00:00Z' + n * interval '1 minute', 60 "
                    "FROM generate_series(1, 2) AS n"
                )
            )
            with pytest.raises(Exception, match="could not create unique index"):
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY ix_heart_rate_provider_time "
                        "ON heart_rate (provider) WHERE provider <> 'GOOGLE'"
                    )
                )
        leftover = _state(engine)["ix_heart_rate_provider_time"]
        assert leftover[:2] == (False, True)  # what an interrupted build leaves

        command.upgrade(alembic_config, MIGRATION.revision)
        state = _state(engine)
        assert set(state) == set(NAMES)
        assert all(valid and not unique for valid, unique, _ in state.values()), state
        heart = state["ix_heart_rate_provider_time"][2]
        assert '(provider, "timestamp")' in heart and "<> 'GOOGLE'" in heart
        long_steps = state["ix_raw_health_data_long_steps"][2]
        assert "CASE" in long_steps and "ELSE true" in long_steps  # bounds checked first
        assert "(source_end_time - source_start_time) > '1 day'::interval" in long_steps
        assert (
            "INCLUDE (new_google_data_point_id, old_fingerprint, new_fingerprint)"
            in (state["ix_raw_health_data_supersessions_type_keys"][2])
        )

        # Run again on valid indexes: nothing is rebuilt; downgrade removes them all.
        command.downgrade(alembic_config, MIGRATION.down_revision)
        assert _state(engine) == {}
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
    finally:
        engine.dispose()


def test_every_build_runs_without_a_timeout(database_url, alembic_config):
    """A role's or database's timeout would cancel a long build half way: none applies."""
    command.upgrade(alembic_config, MIGRATION.down_revision)
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            name = connection.execute(text("SELECT current_database()")).scalar_one()
            connection.execute(text(f"ALTER DATABASE \"{name}\" SET statement_timeout = '1min'"))
            connection.execute(text(f"ALTER DATABASE \"{name}\" SET lock_timeout = '1min'"))
    finally:
        engine.dispose()
    during: list[tuple[str, str]] = []

    def seen(_connection, cursor, statement, *_args) -> None:
        if statement.startswith("CREATE INDEX CONCURRENTLY"):
            during.append(
                tuple(
                    cursor.connection.execute(
                        "SELECT current_setting('statement_timeout'), "
                        "current_setting('lock_timeout')"
                    ).fetchone()
                )
            )

    event.listen(Engine, "before_cursor_execute", seen)
    try:
        command.upgrade(alembic_config, MIGRATION.revision)
    finally:
        event.remove(Engine, "before_cursor_execute", seen)
    assert during == [("0", "0")] * len(NAMES)


def test_an_open_snapshot_leaves_the_indexes_for_later(database_url, alembic_config):
    """A dump begun before the upgrade: no build waits for it (only the 10 s grace), and none
    is left invalid."""
    command.upgrade(alembic_config, MIGRATION.down_revision)
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        dump = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        dump.execute(text("SELECT count(*) FROM raw_health_data"))
        command.upgrade(alembic_config, MIGRATION.revision)
        dump.rollback()
        dump.close()
        assert _state(engine) == {}
        with engine.connect() as connection:
            version = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == MIGRATION.revision
    finally:
        engine.dispose()


def test_a_row_with_far_apart_bounds_does_not_stop_the_upgrade(database_url, alembic_config):
    """Bounds the database accepts but whose difference overflows an interval (and infinite
    ones) are long steps: the long-step index builds over them, and later writes go in."""
    command.upgrade(alembic_config, MIGRATION.down_revision)
    engine = _engine(database_url)
    insert = (
        "INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, "
        "payload_hash, first_seen_at, last_seen_at, source_start_time, source_end_time) "
        "VALUES (gen_random_uuid(), :key, 'steps', '{}', 'x', now(), now(), "
        "CAST(:start AS timestamptz), CAST(:end AS timestamptz))"
    )
    far = [
        ("4713-01-01 00:00:00+00 BC", "294276-12-31 00:00:00+00"),
        ("294276-12-31 00:00:00+00", "4713-01-01 00:00:00+00 BC"),
        ("-infinity", "infinity"),
    ]
    try:
        with engine.connect() as connection:
            for index, (start, end) in enumerate(far):
                connection.execute(
                    text(insert), {"key": f"far-{index}", "start": start, "end": end}
                )
        command.upgrade(alembic_config, MIGRATION.revision)
        assert _state(engine)["ix_raw_health_data_long_steps"][0] is True
        with engine.connect() as connection:
            for index, (start, end) in enumerate(far):
                connection.execute(
                    text(insert), {"key": f"after-{index}", "start": start, "end": end}
                )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM raw_health_data WHERE data_type = 'steps' AND "
                    "(google_data_point_id LIKE 'far-%' OR google_data_point_id LIKE 'after-%')"
                )
            ) == 2 * len(far)
    finally:
        engine.dispose()


def test_verify_names_each_missing_or_invalid_reader_index(database_url, alembic_config):
    """Left for later or interrupted, a reader index is named by verify with the statements
    that build it, one each; with all six valid, verify says nothing about them."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.services.identities import identity_verification

    async def notes() -> list[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                async with AsyncSession(bind=connection) as session:
                    return await identity_verification._reader_index_notes(session)
        finally:
            await engine.dispose()

    command.upgrade(alembic_config, MIGRATION.revision)
    assert asyncio.run(notes()) == []
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "INSERT INTO raw_health_data (id, google_data_point_id, data_type, raw_json, "
                    "payload_hash, first_seen_at, last_seen_at) SELECT gen_random_uuid(), "
                    "'s-' || n, 'steps', '{}', 'x', now(), now() FROM generate_series(1, 2) n"
                )
            )
            connection.execute(text("DROP INDEX ix_heart_rate_provider_time"))
            connection.execute(text("DROP STATISTICS raw_health_data_long_steps"))
            connection.execute(text("DROP INDEX ix_raw_health_data_long_steps"))
            with pytest.raises(Exception, match="could not create unique index"):
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY ix_raw_health_data_long_steps "
                        "ON raw_health_data (data_type) WHERE data_type = 'steps'"
                    )
                )
    finally:
        engine.dispose()
    found = asyncio.run(notes())
    assert len(found) == 3
    assert "statistics raw_health_data_long_steps of 20261116_0041 are missing" in found[2]
    assert "CASE WHEN data_type <> 'steps'" in found[2] and "ANALYZE raw_health_data" in found[2]
    found = found[:2]
    assert "ix_raw_health_data_long_steps of 20261116_0041 is invalid" in found[0]
    assert "ix_heart_rate_provider_time of 20261116_0041 is missing" in found[1]
    for note in found:
        assert "DROP INDEX CONCURRENTLY IF EXISTS" in note
        assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in note
