"""The main line's migrations without the GitHub-only Apple extension, for main-line tests.

The extension adds the real Apple revision (20261107_a001) and its merges with the main line
(20261111_a002, 20261112_a003, 20261112_a004, 20261114_a005). Tests that pin the main line's
own chain (tests/test_legacy_placeholder.py) run in a copy of the checkout's Alembic setup
without those files, whose drift checks compare with the models minus the Apple tables; the
extension's chain is covered by tests/test_apple_legacy_placeholder.py. A test module's
``HEAD`` (the checkout's real head, ``support.migration_chain``) is the main line's head inside
the copy.
"""

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXTENSION_REVISIONS = (
    "20261107_a001_apple_raw.py",
    "20261111_a002_merge_apple_line.py",
    "20261112_a003_merge_apple_line.py",
    "20261112_a004_merge_apple_line.py",
    "20261114_a005_merge_apple_line.py",
)
TARGET = "target_metadata = Base.metadata\n"
MAIN_LINE_TARGET = """from sqlalchemy import MetaData

from app.models import apple

target_metadata = MetaData(naming_convention=Base.metadata.naming_convention)
for _table in Base.metadata.sorted_tables:
    if _table.name not in {*apple.TABLES, apple.IMPORTS, apple.SOURCES}:
        _table.to_metadata(target_metadata)
"""


@pytest.fixture
def main_line_migrations(request, tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("main-line")
    shutil.copytree(
        ROOT / "migrations",
        root / "migrations",
        ignore=shutil.ignore_patterns("__pycache__", *EXTENSION_REVISIONS),
    )
    shutil.copy(ROOT / "alembic.ini", root / "alembic.ini")
    env = root / "migrations" / "env.py"
    text = env.read_text(encoding="utf-8")
    assert text.count(TARGET) == 1, "migrations/env.py no longer names its target metadata"
    env.write_text(text.replace(TARGET, MAIN_LINE_TARGET), encoding="utf-8")
    monkeypatch.chdir(root)
    if hasattr(request.module, "HEAD"):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        (head,) = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
        monkeypatch.setattr(request.module, "HEAD", head)
    return root
