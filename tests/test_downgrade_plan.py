"""Alembic's configured work function still guards losses before an autocommit step."""

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext, RevisionStep
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.db.downgrade_guard import AUTOCOMMIT_REVISION, refuse_lossy_plan


@pytest.mark.parametrize(
    "crosses_commit,upgrade,has_rows",
    [
        (True, False, True),
        (True, False, False),
        (False, False, True),
        (True, True, True),
    ],
)
def test_plan_uses_configured_work_and_only_refuses_a_loss(crosses_commit, upgrade, has_rows):
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    revisions = ["20260930_0005"]
    if crosses_commit:
        revisions.append(AUTOCOMMIT_REVISION)
    steps = [
        RevisionStep(scripts.revision_map, scripts.get_revision(revision), is_upgrade=upgrade)
        for revision in revisions
    ]
    calls = []

    def plan(heads, context):
        calls.append((heads, context))
        return steps

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE manual_measurements (value INTEGER)"))
            if has_rows:
                connection.execute(text("INSERT INTO manual_measurements VALUES (1)"))
            context = MigrationContext.configure(connection, opts={"fn": plan})
            if crosses_commit and not upgrade and has_rows:
                with pytest.raises(RuntimeError, match="manual_measurements holds rows"):
                    refuse_lossy_plan(context)
            else:
                refuse_lossy_plan(context)
            assert calls == [((), context)]
            assert connection.scalar(text("SELECT count(*) FROM manual_measurements")) == has_rows
    finally:
        engine.dispose()


def test_context_without_work_has_no_downgrade_to_guard():
    refuse_lossy_plan(MigrationContext.configure(dialect_name="sqlite"))
