"""The extension's chain around the main line's placeholder (GitHub-only Apple extension).

The main line keeps an empty bridge, 20261109_p001, that answers for databases stamped with the
old placeholder identity 20261107_a001 (tests/test_legacy_placeholder.py, run on the main line's
own revisions). In this tree 20261107_a001 is the real Apple revision: it must stay a revision of
its own, never be translated or skipped, and the single head must be the extension's merge,
20261112_a004 (the latest merge with the main line). Synthetic rows only.
"""

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings
from app.models import apple

APPLE = "20261107_a001"
PLACEHOLDER = "20261109_p001"
BRIDGE_MERGE = "20261109_0029"
MAIN_HEAD = "20261112_0034"
APPLE_MERGE = "20261111_a002"
PREVIOUS_MERGE = "20261112_a003"
HEAD = "20261112_a004"
BRANCHPOINT = "20261107_0025"
NOW = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.fixture
def migration_database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'extension.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    try:
        yield Config("alembic.ini"), engine
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_one_head_and_the_placeholder_in_its_place():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [HEAD]
    assert script.get_revision(HEAD).down_revision == (MAIN_HEAD, PREVIOUS_MERGE)
    assert script.get_revision(PREVIOUS_MERGE).down_revision == ("20261111_0033", APPLE_MERGE)
    assert script.get_revision(APPLE_MERGE).down_revision == (BRIDGE_MERGE, APPLE)
    real = script.get_revision(APPLE)
    assert real.down_revision == BRANCHPOINT
    assert getattr(real.module, "legacy_revision", None) is None
    placeholder = script.get_revision(PLACEHOLDER)
    assert placeholder.down_revision == BRANCHPOINT
    assert placeholder.module.legacy_revision == APPLE
    assert script.get_revision(BRIDGE_MERGE).down_revision == ("20261108_0028", PLACEHOLDER)


def _versions(engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())


def test_a_main_line_install_gains_the_apple_tables(migration_database):
    # A database installed from the main line (at its head, through the empty bridge) upgrades
    # to the extension: the real Apple revision runs, then the merge.
    config, engine = migration_database
    command.upgrade(config, MAIN_HEAD)
    assert not (
        {*apple.TABLES, apple.IMPORTS, apple.SOURCES} & set(inspect(engine).get_table_names())
    )
    command.upgrade(config, "head")
    assert _versions(engine) == {HEAD}
    assert {*apple.TABLES, apple.IMPORTS, apple.SOURCES} <= set(inspect(engine).get_table_names())
    command.check(config)


def test_a_release_stamped_with_the_real_apple_revision_keeps_its_data(migration_database):
    # v0.2.2 as published carried the real 20261107_a001. Its stamp is never translated to the
    # bridge (the revision is installed here) and its Apple rows survive the upgrade.
    config, engine = migration_database
    command.upgrade(config, APPLE)
    assert _versions(engine) == {APPLE}
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO apple_payloads (received_at, channel, headers, body, body_sha256, "
                "body_size) VALUES (:at, 'hae', '{}', X'7B7D', :digest, 2)"
            ),
            {"at": NOW, "digest": "1" * 64},
        )
    command.upgrade(config, "head")
    assert _versions(engine) == {HEAD}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT body FROM apple_payloads")).scalars().all() == [
            b"{}"
        ]
    command.check(config)
    with pytest.raises(RuntimeError, match="holds evidence; downgrade refused"):
        command.downgrade(config, BRANCHPOINT)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM apple_payloads")) == 1
