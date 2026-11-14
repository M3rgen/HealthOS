"""Guard declarations register themselves and preserve the frozen archive coverage."""

from importlib import import_module
from pkgutil import iter_modules

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, create_mock_engine, event
from sqlalchemy.exc import IntegrityError
from support import sqlite_guards

import app.models
from app.db import guards
from app.db.base import Base
from app.models import guards as archive
from app.models import heart_invalidation


def test_archive_guard_counts_and_registered_functions_are_unchanged():
    pg, functions, triggers = guards.definitions()
    sqlite = sqlite_guards.definitions()
    new_pg, new_functions, new_triggers = guards.definitions(heart_invalidation.GUARDS)
    assert len(new_pg) == len(new_triggers) == 26
    assert len(new_functions) == 17
    # Keep the pre-projection archive contract fixed; verify the new declaration separately.
    # GitHub-only Apple extension: two append-only tables, two triggers each per backend.
    original = {name: value for name, value in pg.items() if name not in new_pg}
    assert len(original) == len(set(triggers) - set(new_triggers)) == 105
    assert len(sqlite) == 100
    assert len(functions.keys() - new_functions.keys()) == 27
    assert len({table for table, _ in original.values()}) == 44
    assert len({table for table, _ in sqlite.values()}) == 43
    assert (pg, functions, triggers) == archive.definitions()
    assert sqlite == sqlite_guards.SQLITE_TRIGGER_SQL
    assert {function for _, _, function in triggers} == functions.keys()


def test_append_only_registration_needs_no_module_list_and_refuses_sqlite_changes(monkeypatch):
    monkeypatch.setattr(guards, "REGISTRY", [])
    declarations = guards.GuardSet()
    declarations.append_only("synthetic_archive")
    pg, _, triggers = guards.definitions()
    sqlite = sqlite_guards.definitions()
    assert guards.definitions(declarations) == (pg, {}, triggers)
    assert set(pg) == {"synthetic_archive_append_only", "synthetic_archive_no_truncate"}
    assert set(sqlite) == {"synthetic_archive_no_update", "synthetic_archive_no_delete"}
    assert {function for _, _, function in triggers} == {"healthos_refuse_change"}
    assert all("ENABLE ALWAYS" in sql for sql in declarations.postgresql_ddl[2:])

    metadata = MetaData()
    table = Table("synthetic_archive", metadata, Column("id", Integer, primary_key=True))
    event.listen(metadata, "after_create", guards.create_guards)
    event.listen(metadata, "after_create", sqlite_guards.create_guards)
    engine = create_engine("sqlite://")
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(table.insert().values(id=1))
        for statement in (table.update().values(id=2), table.delete()):
            with engine.begin() as connection:
                with pytest.raises(IntegrityError, match="synthetic_archive keeps every record"):
                    connection.execute(statement)
        with engine.connect() as connection:
            assert connection.execute(table.select()).all() == [(1,)]
    finally:
        engine.dispose()


def test_late_shared_function_registration_precedes_postgresql_triggers(monkeypatch):
    monkeypatch.setattr(guards, "REGISTRY", [])
    declarations = guards.GuardSet()
    declarations.append_only("synthetic_archive")
    guards.GuardSet(functions={"healthos_refuse_change": "BEGIN RAISE EXCEPTION 'refused'; END"})
    statements = []
    engine = create_mock_engine(
        "postgresql://",
        lambda sql, *args, **kw: statements.append(str(sql.compile(dialect=engine.dialect))),
    )
    # Use the real metadata listener; the function is declared after its first caller.
    Base.metadata.create_all(engine, checkfirst=False)
    ddl = [sql for sql in statements if "synthetic_archive" in sql or "CREATE FUNCTION" in sql]
    assert ddl[0].startswith("CREATE FUNCTION healthos_refuse_change()")
    assert sum(sql.startswith("CREATE TRIGGER") for sql in ddl) == 2
    assert sum("ENABLE ALWAYS TRIGGER" in sql for sql in ddl) == 2


@pytest.mark.parametrize("attribute", ["pg", "functions"])
def test_duplicate_registration_is_rejected_before_any_ddl(monkeypatch, attribute):
    monkeypatch.setattr(guards, "REGISTRY", [])
    first, second = guards.GuardSet(), guards.GuardSet()
    getattr(first, attribute)["duplicate"] = "synthetic"
    getattr(second, attribute)["duplicate"] = "synthetic"
    statements = []
    engine = create_mock_engine(
        "postgresql://", lambda sql, *args, **kw: statements.append(str(sql))
    )
    with pytest.raises(ValueError, match="Duplicate archive guard definitions.*duplicate"):
        guards.create_guards(None, engine)
    assert not statements


def test_duplicate_test_guard_registration_is_rejected_before_any_ddl(monkeypatch):
    monkeypatch.setattr(guards, "REGISTRY", [])
    for _ in range(2):
        guards.GuardSet().append_only("synthetic_archive")
    statements = []
    engine = create_mock_engine("sqlite://", lambda sql, *args, **kw: statements.append(str(sql)))
    with pytest.raises(ValueError, match="Duplicate archive guard definitions.*synthetic_archive"):
        sqlite_guards.create_guards(None, engine)
    assert not statements


@pytest.mark.parametrize("name", ["PG_TRIGGER_SQL", "PROTECTIVE_TRIGGERS", "SQLITE_TRIGGER_SQL"])
def test_model_modules_do_not_export_trigger_tables(name):
    for module in iter_modules(app.models.__path__, prefix="app.models."):
        assert not hasattr(import_module(module.name), name), module.name
