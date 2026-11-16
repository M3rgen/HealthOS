"""Additive, disposable track schema: no guards, no references, a downgrade only drops it.

Covers the packages (0039) and their workout links (0040).
"""

import re
from importlib import import_module

import pytest
from alembic import command
from alembic.config import Config
from fit_factory import track_activity
from sqlalchemy import inspect, text
from support.apple_main_line import main_line_migrations  # noqa: F401
from test_garmin_files_migration import insert_evidence
from test_garmin_migration import SCHEMA
from test_garmin_migration import database as database

from app.core.config import get_settings
from app.db import guards
from app.db.downgrade_guard import DISPOSABLE_TABLES
from app.models import garmin_files
from app.models.garmin_tracks import TABLES, GarminTrack, GarminTrackSession
from app.services.providers.fit_tracks import METHOD_VERSION, derive_track

PREVIOUS = "20261115_0038"
REVISION = "20261115_0039"
LINKS = "20261115_0040"
MIGRATION = import_module("migrations.versions.20261115_0039_garmin_tracks")

# GitHub-only Apple extension: these tests pin the main line's chain; in this tree "head" also
# joins the Apple line, whose tables a main-line downgrade keeps (support/apple_main_line.py).
pytestmark = pytest.mark.usefixtures("main_line_migrations")


def insert_track(connection):
    connection.execute(
        GarminTrack.__table__.insert().values(
            source_sha256="a" * 64,
            method_version=METHOD_VERSION,
            status="ready",
            **vars(derive_track(track_activity(3))),
        )
    )


def test_track_upgrade_adds_only_its_table_and_preserves_existing_files(database):
    config, engine = database
    command.upgrade(config, PREVIOUS)
    with engine.begin() as connection:
        insert_evidence(connection)
        before = connection.execute(text("SELECT * FROM garmin_activity_files")).all()
        schema = set(connection.execute(SCHEMA).tuples())
    tables = set(inspect(engine).get_table_names())
    command.upgrade(config, "head")
    command.check(config)
    assert set(inspect(engine).get_table_names()) - tables == set(TABLES)
    with engine.connect() as connection:
        assert schema <= set(connection.execute(SCHEMA).tuples())
        assert connection.execute(text("SELECT * FROM garmin_activity_files")).all() == before
    command.downgrade(config, PREVIOUS)
    assert set(inspect(engine).get_table_names()) == tables
    with engine.connect() as connection:
        assert set(connection.execute(SCHEMA).tuples()) == schema


def test_populated_packages_are_rewritable_and_downgrade_keeps_every_source(database):
    config, engine = database
    command.upgrade(config, "head")
    with engine.begin() as connection:
        insert_evidence(connection)
        insert_track(connection)
        # A read model: maintenance may rewrite or remove packages.
        connection.execute(text("UPDATE garmin_tracks SET sample_count = sample_count"))
        sources = {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in garmin_files.TABLES
        }
    command.downgrade(config, PREVIOUS)
    assert "garmin_tracks" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        for table, rows in sources.items():
            assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows
    assert sources["garmin_activity_files"]


def test_track_table_is_classified_disposable_and_unguarded():
    assert set(TABLES) <= DISPOSABLE_TABLES
    assert not set(garmin_files.TABLES) & DISPOSABLE_TABLES
    _, _, triggers = guards.definitions()
    assert not {table for table, _, _ in triggers} & set(TABLES)


def test_postgres_track_ddl_is_additive_and_offline_downgrade_only_drops_it(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), f"{PREVIOUS}:{REVISION}", sql=True)
        upgrade = capsys.readouterr().out
        command.downgrade(Config("alembic.ini"), f"{REVISION}:{PREVIOUS}", sql=True)
        downgrade = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert upgrade.count("CREATE TABLE ") == 1 and "samples BYTEA NOT NULL" in upgrade
    assert "REFERENCES" not in upgrade and "TRIGGER" not in upgrade
    assert not any(op in upgrade for op in ("UPDATE garmin_", "DELETE FROM ", "DROP "))
    assert re.findall(r"DROP TABLE (\w+)", downgrade) == ["garmin_tracks"]
    assert MIGRATION.down_revision == PREVIOUS


def test_session_links_add_one_table_and_a_populated_downgrade_keeps_packages(database):
    config, engine = database
    command.upgrade(config, REVISION)
    with engine.begin() as connection:
        insert_evidence(connection)
        insert_track(connection)
        packages = connection.execute(text("SELECT * FROM garmin_tracks")).all()
    command.upgrade(config, LINKS)
    command.check(config)
    engine.dispose()  # A pooled SQLite connection may still hold the schema before 0040.
    assert {index["name"] for index in inspect(engine).get_indexes("garmin_track_sessions")} == {
        "ix_garmin_track_sessions_source"
    }
    with engine.begin() as connection:
        connection.execute(
            GarminTrackSession.__table__.insert().values(
                source_record_id="fit:synthetic", source_sha256="a" * 64
            )
        )
    command.downgrade(config, REVISION)
    assert "garmin_track_sessions" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM garmin_tracks")).all() == packages


def test_postgres_link_ddl_is_additive_and_offline_downgrade_only_drops_it(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://health:health@postgres/health")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), f"{REVISION}:{LINKS}", sql=True)
        upgrade = capsys.readouterr().out
        command.downgrade(Config("alembic.ini"), f"{LINKS}:{REVISION}", sql=True)
        downgrade = capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert re.findall(r"CREATE TABLE (\w+)", upgrade) == ["garmin_track_sessions"]
    assert "REFERENCES" not in upgrade and "TRIGGER" not in upgrade
    assert not any(op in upgrade for op in ("UPDATE garmin_", "DELETE FROM ", "DROP "))
    assert re.findall(r"DROP TABLE (\w+)", downgrade) == ["garmin_track_sessions"]
