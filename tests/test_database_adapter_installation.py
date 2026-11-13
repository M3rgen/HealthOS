"""SQLite support is opt-in and cannot alter the production PostgreSQL schema or path."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.dialect import database_operation
from app.sync.ownership import RUN_NAMESPACE, _PostgresLock, _run_lock


@pytest.mark.parametrize("backend", ["postgresql", "sqlite", "unsupported"])
def test_ownership_dispatch_supports_keyword_engines_and_rejects_unknown_backends(backend):
    from support.sqlite_ownership import _SqliteLock

    engine = Mock(spec=AsyncEngine)
    engine.dialect = SimpleNamespace(name=backend)
    engine.url = SimpleNamespace(database=":memory:")
    if backend == "unsupported":
        with pytest.raises(RuntimeError, match="Unsupported database dialect: unsupported"):
            _run_lock(engine=engine, namespace=RUN_NAMESPACE, key="synthetic")
        return
    lock = _run_lock(engine=engine, namespace=RUN_NAMESPACE, key="synthetic")
    assert isinstance(lock, _PostgresLock if backend == "postgresql" else _SqliteLock)
    engine.connect.assert_not_called()
    if backend == "sqlite":
        with pytest.raises(KeyError, match="<lambda>"):
            database_operation(lambda engine: pytest.fail("unregistered operation"))(engine)


@pytest.mark.parametrize("backend", ["postgresql", "sqlite"])
def test_offline_downgrade_dispatch_keeps_postgres_data_loss_protection(backend):
    from io import StringIO

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.db.downgrade_guard import _refuse_while_held

    output = StringIO()
    context = MigrationContext.configure(
        dialect_name=backend, opts={"as_sql": True, "output_buffer": output}
    )
    _refuse_while_held(
        Operations(context), "synthetic_archive", None, "evidence IS NOT NULL", "row"
    )
    statements = output.getvalue()
    if backend == "sqlite":
        assert statements == ""
    else:
        assert "LOCK TABLE synthetic_archive IN ACCESS EXCLUSIVE MODE" in statements
        assert (
            "IF EXISTS (SELECT 1 FROM synthetic_archive WHERE evidence IS NOT NULL)" in statements
        )
        assert "RAISE EXCEPTION 'row; downgrade refused: it would lose data" in statements
