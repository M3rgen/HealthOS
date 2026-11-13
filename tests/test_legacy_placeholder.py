"""Separate bridge identity, legacy stamps and a synthetic extension; no real health data."""

import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, text
from support.apple_main_line import main_line_migrations  # noqa: F401
from support.migration_chain import HEAD

from app.core.config import get_settings

LEGACY = "20261107_a001"
PLACEHOLDER = "20261109_p001"
# The merge revision of the Apple line; later revisions build on it (HEAD is the real head).
MERGE = "20261109_0029"

# GitHub-only Apple extension: the main line's chain, without the extension's revisions
# (EXTENSION_REVISIONS in support/apple_main_line.py).
pytestmark = pytest.mark.usefixtures("main_line_migrations")


@pytest.fixture
def migration_database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_placeholder_has_its_own_identity():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    revisions = {revision.revision for revision in script.walk_revisions()}
    assert LEGACY not in revisions
    placeholder = script.get_revision(PLACEHOLDER)
    assert placeholder.module.legacy_revision == LEGACY
    assert placeholder.down_revision == "20261107_0025"
    assert script.get_revision(MERGE).down_revision == ("20261108_0028", PLACEHOLDER)


def test_upgraded_legacy_placeholder_has_no_schema_drift(migration_database):
    config, engine = migration_database
    command.upgrade(config, PLACEHOLDER)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :legacy"), {"legacy": LEGACY}
        )
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.parametrize("stamp", [LEGACY, PLACEHOLDER, "20261108_0028", MERGE])
def test_upgrade_preserves_existing_rows_schema_and_guards(migration_database, stamp):
    config, engine = migration_database
    command.upgrade(config, PLACEHOLDER if stamp == LEGACY else stamp)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :stamp"), {"stamp": stamp}
        )
        connection.execute(text("CREATE TABLE apple_payloads (id INTEGER PRIMARY KEY, body BLOB)"))
        connection.execute(text("INSERT INTO apple_payloads VALUES (1, X'010203')"))
        connection.execute(
            text(
                "CREATE TRIGGER preserve_payload BEFORE DELETE ON apple_payloads "
                "BEGIN SELECT RAISE(ABORT, 'keeps every record'); END"
            )
        )
        schema = connection.execute(
            text("SELECT name, sql FROM sqlite_master WHERE tbl_name = 'apple_payloads'")
        ).all()
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        assert connection.execute(text("SELECT * FROM apple_payloads")).all() == [
            (1, b"\x01\x02\x03")
        ]
        assert (
            connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE tbl_name = 'apple_payloads'")
            ).all()
            == schema
        )
    command.downgrade(config, PLACEHOLDER)
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM apple_payloads")).all() == [
            (1, b"\x01\x02\x03")
        ]


@pytest.mark.parametrize("already_applied", [False, True])
def test_real_extension_revision_is_never_shadowed_or_skipped(
    migration_database, tmp_path, already_applied
):
    config, engine = migration_database
    location = tmp_path / "migrations"
    shutil.copytree(Path("migrations"), location)
    config.set_main_option("script_location", str(location))
    (location / "versions" / "synthetic_extension.py").write_text(
        f"from alembic import op\nrevision = '{LEGACY}'\ndown_revision = '20261107_0025'\n"
        "def upgrade():\n    op.execute('CREATE TABLE synthetic_extension (id INTEGER)')\n"
        "    op.execute('INSERT INTO synthetic_extension VALUES (1)')\n"
        "def downgrade():\n    raise RuntimeError('data loss refused')\n"
    )
    (location / "versions" / "synthetic_merge.py").write_text(
        f"revision = 'synthetic_merge'\ndown_revision = ('{HEAD}', '{LEGACY}')\n"
        "def upgrade():\n    pass\ndef downgrade():\n    pass\n"
    )
    if already_applied:
        command.upgrade(config, LEGACY)
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version")) == "synthetic_merge"
        )
        assert connection.execute(text("SELECT * FROM synthetic_extension")).all() == [(1,)]
    # The bridge never substitutes for a real extension's data-preserving downgrade.
    with pytest.raises(RuntimeError, match="data loss refused"):
        command.downgrade(config, "20261107_0025")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM synthetic_extension")).all() == [(1,)]


def test_unknown_stamp_still_refuses_to_upgrade(migration_database):
    config, engine = migration_database
    command.upgrade(config, PLACEHOLDER)
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'unknown_revision'"))
    with pytest.raises(
        CommandError, match="Can't locate revision identified by 'unknown_revision'"
    ):
        command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version")) == "unknown_revision"
        )
