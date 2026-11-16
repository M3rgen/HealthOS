"""PostgreSQL original-file migration, append-only guards, ownership and actual storage."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from support.apple_main_line import main_line_migrations  # noqa: F401
from support.garmin_files import NOW, PREFIX, pull_service, seed_activities
from test_garmin_files_migration import PREVIOUS, insert_evidence

from app.models.garmin import GarminRawRecord
from app.models.garmin_files import TABLES
from app.models.garmin_tracks import TABLES as TRACK_TABLES
from app.services.providers.garmin_file_archive import file_counts
from app.sync.ownership import garmin_ownership

pytestmark = pytest.mark.integration


# GitHub-only Apple extension: the main line's chain (support/apple_main_line.py); in this tree
# "head" also joins the Apple line, whose tables a main-line downgrade keeps.
@pytest.mark.usefixtures("main_line_migrations")
def test_pg_empty_file_migration_cycle_preserves_raw_tables(database_url, alembic_config):
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))
    try:
        command.upgrade(alembic_config, PREVIOUS)
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
            raw = connection.execute(text("SELECT * FROM garmin_raw_records")).all()
        before = set(inspect(engine).get_table_names())
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        assert set(inspect(engine).get_table_names()) - before == set(TABLES + TRACK_TABLES)
        command.downgrade(alembic_config, PREVIOUS)
        assert set(inspect(engine).get_table_names()) == before
        with engine.connect() as connection:
            assert connection.execute(text("SELECT * FROM garmin_raw_records")).all() == raw
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
    finally:
        engine.dispose()


@pytest.mark.parametrize("unavailable", [False, True])
def test_pg_files_and_absence_refuse_changes_and_populated_downgrade(
    database_url, migrated, unavailable
):
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))
    table = TABLES[int(unavailable)]
    try:
        with engine.begin() as connection:
            insert_evidence(connection, unavailable=unavailable)
        for role in ("origin", "replica"):
            for sql in (
                f"UPDATE {table} SET activity_id = 'x'",
                f"DELETE FROM {table}",
                "TRUNCATE garmin_activity_files, garmin_activity_file_results",
            ):
                with engine.connect() as connection:
                    transaction = connection.begin()
                    connection.execute(text(f"SET LOCAL session_replication_role = {role}"))
                    try:
                        with pytest.raises(DBAPIError, match="keeps every record"):
                            connection.execute(text(sql))
                    finally:
                        transaction.rollback()
        with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
            command.downgrade(migrated, PREVIOUS)
        assert set(TABLES) <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    finally:
        engine.dispose()


async def test_pg_scheduled_and_cli_owners_download_each_activity_once(pg_sessions, tmp_path):
    await seed_activities(pg_sessions, 4)
    services = [pull_service(pg_sessions, tmp_path) for _ in range(2)]
    pulls = [service[0] for service in services]
    for pull in pulls:
        pull.owned_by(garmin_ownership(pg_sessions))
    try:
        reports = await asyncio.gather(*(pull.activity_files(4) for pull in pulls))
        assert sum(r.downloaded for r in reports) == sum(r.requests for r in reports) == 4
        async with pg_sessions() as session:
            assert await file_counts(session) == {
                "downloaded": 4,
                "pending": 0,
                "unavailable": 0,
                "invalid": 0,
            }
        requests = [request for _, server, _ in services for request in server.downloads()]
        assert len(requests) == 4
        assert {r.url.path for r in requests} == {PREFIX + str(i) for i in range(1, 5)}
    finally:
        for pull in pulls:
            await pull.aclose()
