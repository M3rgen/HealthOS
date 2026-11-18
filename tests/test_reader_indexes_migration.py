"""20261116_0041: a concurrent build that stops half way is cleaned up and retried.

The PostgreSQL side (a real invalid leftover, timeouts lifted) is
tests/integration/test_pg_reader_indexes_migration.py; this drives the retry logic alone.
"""

import ast
from importlib import import_module
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

MIGRATION = import_module("migrations.versions.20261116_0041_reader_indexes")
NAME = "ix_example"
STATEMENT = "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_example ON example (a) WHERE b"


class Bind:
    """The catalog state of one index name: None absent, False invalid, True valid."""

    def __init__(self, state=None, failures=0):
        self.state, self.failures, self.statements = state, failures, []

    busy = 0

    def scalar(self, statement, _parameters=None):
        if "pg_stat_activity" in str(statement):
            return self.busy
        return self.state

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql.split()[0])
        if sql.startswith("DROP INDEX CONCURRENTLY"):
            self.state = None
        elif sql.startswith("CREATE INDEX CONCURRENTLY"):
            if self.failures:
                self.failures -= 1
                self.state = False  # what a build stopped half way leaves
                raise DBAPIError(sql, {}, RuntimeError("deadlock detected"))
            self.state = True


@pytest.mark.parametrize(
    "state, failures, statements",
    [
        (True, 0, []),
        (None, 0, ["CREATE"]),
        (False, 0, ["DROP", "CREATE"]),
        (None, 1, ["CREATE", "DROP", "CREATE"]),
        (False, 2, ["DROP", "CREATE", "DROP", "CREATE", "DROP", "CREATE"]),
    ],
)
def test_an_invalid_index_is_dropped_and_built_again(state, failures, statements):
    bind = Bind(state, failures)
    MIGRATION.build(bind, NAME, STATEMENT)
    assert bind.statements == statements
    assert bind.state is True


def test_the_last_failure_drops_its_leftover_and_stops_the_upgrade():
    bind = Bind(None, failures=MIGRATION.ATTEMPTS)
    with pytest.raises(RuntimeError, match="invalid leftover was dropped"):
        MIGRATION.build(bind, NAME, STATEMENT)
    assert bind.statements == ["CREATE", "DROP"] * MIGRATION.ATTEMPTS
    assert bind.state is None  # no invalid index stays behind


def test_no_build_starts_while_a_client_session_holds_a_snapshot(monkeypatch, capsys):
    """A dump's snapshot would hold a build until the dump ends: the indexes wait instead,
    each statement printed on a line of its own (psql -c runs one at a time)."""
    monkeypatch.setattr(MIGRATION.time, "sleep", lambda _seconds: None)
    clock = iter(range(1000))
    monkeypatch.setattr(MIGRATION.time, "monotonic", lambda: next(clock))
    busy = Bind(None)
    busy.busy = 1
    later = MIGRATION.build_all(busy)
    assert busy.statements == [] and len(later) == 2 * len(MIGRATION.INDEXES)
    assert later[0] == f"DROP INDEX CONCURRENTLY IF EXISTS {MIGRATION.INDEXES[0][0]};"
    assert later[1] == MIGRATION.create_statement(*MIGRATION.INDEXES[0]) + ";"
    printed = capsys.readouterr().out
    assert printed.startswith("WARNING 20261116_0041: indexes left for later")
    assert all(f"\n{statement}" in printed for statement in later)
    assert all(statement.count(";") == 1 for statement in later)
    quiet = Bind(None)
    assert MIGRATION.build_all(quiet, MIGRATION.INDEXES[:1]) == []
    assert quiet.statements == ["CREATE"]


def test_only_client_sessions_count_as_busy():
    """A concurrent build does not wait for autovacuum: only client backends hold it back."""
    assert "a.backend_type = 'client backend'" in MIGRATION.BUSY_SQL


def test_the_long_steps_index_is_built_first_and_matches_the_reader():
    """Without it the reader keeps the old window: it goes first. Its predicate is the
    reader's long-step condition word for word, so the planner can use it."""
    from app.services.identities.identity_verification import READER_INDEXES
    from app.services.readers.measurement_windows import LONG_STEP_SQL, LONG_STEPS_INDEX

    name, _table, _columns, _include, where = MIGRATION.INDEXES[0]
    assert name == LONG_STEPS_INDEX
    assert where == LONG_STEP_SQL
    assert set(READER_INDEXES) == {index[0] for index in MIGRATION.INDEXES}


def test_statements_match_the_models():
    from app.db.base import Base

    for name, table, columns, include, where in MIGRATION.INDEXES:
        (index,) = [i for i in Base.metadata.tables[table].indexes if i.name == name]
        keys = [str(expression).removeprefix(f"{table}.") for expression in index.expressions]
        assert [key.lower() for key in keys] == [column.lower() for column in columns]
        assert list(index.dialect_options["postgresql"]["include"] or ()) == list(include)
        assert str(index.dialect_options["postgresql"]["where"]) == where
        assert str(index.dialect_options["sqlite"]["where"]) == MIGRATION.SQLITE_WHERE.get(
            name, where
        )
        assert "CONCURRENTLY" in MIGRATION.create_statement(name, table, columns, include, where)


def test_upgrade_drops_only_invalid_leftovers_of_its_own_indexes():
    """Rule 3 (no DROP outside downgrade): an upgrade may drop only what holds no data, an
    invalid index of its own names left by an interrupted build, as 20261017_0007 drops its
    ad-hoc index. Every executed DROP outside downgrade() is drop_invalid's, behind its
    validity check; build_all only prints DROP statements for the operator."""
    source = Path(MIGRATION.__file__).read_text()
    tree = ast.parse(source)
    executed = {
        function.name
        for function in tree.body
        if isinstance(function, ast.FunctionDef) and function.name != "downgrade"
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "DROP " in node.value
    }
    assert executed <= {"drop_invalid", "build_all"}
    drop_invalid = next(f for f in tree.body if getattr(f, "name", None) == "drop_invalid")
    assert "validity(bind, name) is False" in ast.unparse(drop_invalid)
    build_all = next(f for f in tree.body if getattr(f, "name", None) == "build_all")
    assert "execute" not in ast.unparse(build_all)
