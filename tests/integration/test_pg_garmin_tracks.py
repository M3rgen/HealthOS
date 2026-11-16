"""PostgreSQL track fills converge, never touch raw inputs, and a downgrade drops only them."""

import asyncio

import pytest
from alembic import command
from fit_factory import track_activity
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from support.apple_main_line import main_line_migrations  # noqa: F401
from support.garmin_files import seed_activities
from test_garmin_files_migration import insert_evidence
from test_garmin_tracks import download, packages
from test_garmin_tracks_migration import PREVIOUS, insert_track

from app.models.garmin_files import TABLES as FILE_TABLES
from app.models.garmin_files import GarminActivityFile
from app.models.garmin_tracks import TABLES as TRACK_TABLES
from app.models.garmin_tracks import GarminTrack, GarminTrackSession
from app.services.providers import garmin_tracks

pytestmark = pytest.mark.integration


async def test_pg_concurrent_method_rebuilds_keep_both_versions(pg_sessions, monkeypatch):
    await seed_activities(pg_sessions, 2)
    for key in (1, 2):
        await download(pg_sessions, track_activity(5 + key), key=key)
    async with pg_sessions() as session:
        before = list(
            await session.scalars(select(GarminActivityFile.body).order_by(GarminActivityFile.id))
        )
    monkeypatch.setattr(garmin_tracks, "METHOD_VERSION", garmin_tracks.METHOD_VERSION + 1)
    results = await asyncio.gather(*(garmin_tracks.rebuild_tracks(pg_sessions) for _ in range(2)))
    assert sum(results) == 2
    assert len(await packages(pg_sessions)) == 4
    assert await garmin_tracks.rebuild_tracks(pg_sessions) == 0
    async with pg_sessions() as session:
        bodies = select(GarminActivityFile.body).order_by(GarminActivityFile.id)
        assert list(await session.scalars(bodies)) == before
        assert set(await session.scalars(select(GarminTrack.sample_count))) == {6, 7}


async def test_pg_concurrent_projection_of_one_content_adds_one_package(pg_sessions):
    content = track_activity(9)

    async def project():
        async with pg_sessions.begin() as session:
            return await garmin_tracks.project_track(session, content)

    # ON CONFLICT waits for the first committed insert; the others add nothing.
    assert sum(await asyncio.gather(*(project() for _ in range(4)))) == 1
    assert len(await packages(pg_sessions)) == 1


# GitHub-only Apple extension: the main line's chain (support/apple_main_line.py); in this tree
# "head" also joins the Apple line, whose tables a main-line downgrade keeps.
@pytest.mark.usefixtures("main_line_migrations")
def test_pg_populated_track_downgrade_drops_only_the_read_model(database_url, alembic_config):
    engine = create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))
    try:
        command.upgrade(alembic_config, PREVIOUS)
        before = set(inspect(engine).get_table_names())
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
        assert set(inspect(engine).get_table_names()) - before == set(TRACK_TABLES)
        with engine.begin() as connection:
            insert_evidence(connection)
            insert_track(connection)
            connection.execute(
                GarminTrackSession.__table__.insert().values(
                    source_record_id="fit:synthetic", source_sha256="a" * 64
                )
            )
            sources = {
                table: connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all()
                for table in FILE_TABLES
            }
        command.downgrade(alembic_config, PREVIOUS)
        assert set(inspect(engine).get_table_names()) == before
        with engine.connect() as connection:
            for table, rows in sources.items():
                assert connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).all() == rows
        command.upgrade(alembic_config, "head")
        command.check(alembic_config)
    finally:
        engine.dispose()
