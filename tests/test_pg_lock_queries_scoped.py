"""Every pg_locks query names this database or one backend.

pg_locks spans the whole server, but advisory locks belong to one database. Integration tests
run in parallel, each in its own database on one server, with the same lock keys: a query
without a scope sees (and may terminate) another worker's owner. The Garmin connection-loss
tests did exactly that and failed whenever two workers held the same kind at once.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("current_database()", "pg_backend_pid()", "pid = :")


def lock_queries():
    for folder in ("app", "migrations", "tests"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                # Adjacent literals are one Constant, so this is the whole statement.
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "FROM pg_locks" in node.value:
                        yield f"{path.relative_to(ROOT)}:{node.lineno}", node.value


def test_lock_queries_are_found():
    assert sum(1 for _ in lock_queries()) >= 10


def test_every_lock_query_is_scoped_to_this_database_or_one_backend():
    unscoped = [where for where, sql in lock_queries() if not any(s in sql for s in SCOPES)]
    assert unscoped == []
