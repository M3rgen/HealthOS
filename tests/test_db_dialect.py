"""The shared UPSERT helper retains both dialects and caller-owned transactions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dialect import dialect_insert
from app.models.state import FetchRound, fetch_round_start


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_dialect_insert_supports_both_conflict_actions(dialect):
    session = Mock(spec=AsyncSession)
    session.get_bind.return_value.dialect = dialect
    statement = dialect_insert(session, FetchRound.__table__).values(
        kind="sync", key="steps", started_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    ignored = statement.on_conflict_do_nothing(index_elements=["kind", "key"])
    updated = statement.on_conflict_do_update(
        index_elements=["kind", "key"], set_={"started_at": statement.excluded.started_at}
    )
    target = f"(kind, {dialect.identifier_preparer.quote('key')})"
    assert f"ON CONFLICT {target} DO NOTHING" in str(ignored.compile(dialect=dialect))
    assert f"ON CONFLICT {target} DO UPDATE SET started_at = excluded.started_at" in str(
        updated.compile(dialect=dialect)
    )


def test_dialect_insert_rejects_unsupported_database():
    session = Mock(spec=AsyncSession)
    session.get_bind.return_value.dialect.name = "unsupported"
    with pytest.raises(RuntimeError, match="^Unsupported database dialect: unsupported$"):
        dialect_insert(session, FetchRound.__table__)


async def test_fetch_round_resume_restart_and_rollback(sessions):
    first = datetime(2026, 1, 1, tzinfo=UTC)
    later = first + timedelta(hours=1)
    async with sessions.begin() as session:
        assert await fetch_round_start(session, "sync", "steps", fresh=True, now=first) == first
    async with sessions() as session:
        resumed = await fetch_round_start(session, "sync", "steps", fresh=False, now=later)
        assert resumed.replace(tzinfo=UTC) == first
        assert await fetch_round_start(session, "sync", "steps", fresh=True, now=later) == later
        await session.flush()
        await fetch_round_start(session, "sync", "heart_rate", fresh=True, now=later)
        await session.rollback()
    async with sessions() as session:
        stored = await session.get(FetchRound, ("sync", "steps"))
        assert stored.started_at.replace(tzinfo=UTC) == first
        assert await session.get(FetchRound, ("sync", "heart_rate")) is None


def test_production_imports_do_not_install_sqlite():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from types import SimpleNamespace
from sqlalchemy import create_mock_engine
import app.models
from app.db.base import Base
from app.db.dialect import DATABASES, for_bind

def ddl(backend):
    statements = []
    engine = create_mock_engine(backend + '://', lambda sql, *a, **k:
                statements.append(str(sql.compile(dialect=engine.dialect))))
    Base.metadata.create_all(engine, checkfirst=False)
    return statements

before = ddl('postgresql')
assert not any(sql.startswith(('CREATE TRIGGER', 'CREATE VIEW')) for sql in ddl('sqlite'))
assert set(DATABASES) == {'postgresql'}
try:
    for_bind(SimpleNamespace(dialect=SimpleNamespace(name='sqlite')))
except RuntimeError as exc:
    assert str(exc) == 'Unsupported database dialect: sqlite'
else:
    raise AssertionError('SQLite was installed by production imports')
sys.path.insert(0, 'tests')
from support.sqlite_database import install
install()
assert ddl('postgresql') == before
assert sum(sql.startswith('CREATE TRIGGER') for sql in ddl('sqlite')) == 96  # + Apple's 4
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def test_postgres_controls_and_retry_scope_keep_the_callers_transaction():
    from unittest.mock import AsyncMock

    from sqlalchemy import Integer, column, text

    from app.db.dialect import database

    session = Mock(spec=AsyncSession)
    session.get_bind.return_value.dialect = postgresql.dialect()
    session.execute = AsyncMock()
    adapter = database(session)
    statement = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    await adapter.control(session, statement)
    session.execute.assert_awaited_once_with(statement, None)
    assert adapter.attempt_scope(session, 8) == (8, session.begin_nested)
    criterion = adapter.among(column("value", Integer()), list(range(5_000)))
    compiled = criterion.compile(dialect=postgresql.dialect())
    assert "ANY" in str(compiled) and len(compiled.params) == 1
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.parametrize(
    "row, expected",
    [
        ((True, True, True, True), "superuser"),
        ((False, True, False, False), "privileged_role"),
        ((False, False, True, False), "privileged_role"),
        ((False, True, True, True), "privileged_role"),
        ((False, False, False, True), "schema_owner"),
        ((False, False, False, False), None),
        (None, None),
    ],
    ids=["superuser", "attribute", "borrowed", "precedence", "owner", "restricted", "no-role"],
)
async def test_database_dispatch_accepts_keyword_sessions(row, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db.role_check import database_role_risk

    session = Mock(spec=AsyncSession)
    session.get_bind.return_value.dialect = postgresql.dialect()
    session.execute = AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
    assert await database_role_risk(session=session) == expected
    session.execute.assert_awaited_once()


async def test_database_clock_shares_the_application_test_clock(monkeypatch):
    from support.clock import freeze_app_clock

    from app.services.identities.source_chain_repository import database_now

    session = Mock(spec=AsyncSession)
    session.get_bind.return_value.dialect = sqlite.dialect()
    at = datetime(2026, 1, 1, tzinfo=UTC)
    freeze_app_clock(monkeypatch, at)
    assert await database_now(session) == at
