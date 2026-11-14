"""Identity model imports share one ORM registry and one set of archive guards."""

import subprocess
import sys
import textwrap

import pytest

COLD_IMPORT_SCRIPT = textwrap.dedent(
    """
    import importlib
    import sys
    from collections import Counter
    from sqlalchemy import create_engine, create_mock_engine, text

    importlib.import_module(sys.argv[1])
    sys.path.insert(0, "tests")
    from app.db.base import Base
    from app.db.guards import REGISTRY
    from app.models import identity, identity_constants, identity_guards, identity_tables
    from app.models import derived_invalidation, manual_flags, raw_versions, retrieval
    from app.models import apple, garmin, oauth_credentials, raw_rejections, webhook_status
    from app.models import heart_invalidation

    assert not any(name.startswith('support.') for name in sys.modules)
    from support import sqlite_guards
    from support.sqlite_database import install
    install()
    installed = tuple(Base.metadata.dispatch.after_create)
    install()
    assert tuple(Base.metadata.dispatch.after_create) == installed
    assert 'app.services.identities.identity' not in sys.modules
    assert 'app.services.archive.raw_history' not in sys.modules
    listeners = tuple(Base.metadata.dispatch.after_create)
    mappers = set(Base.registry.mappers)
    tables = dict(Base.metadata.tables)
    for name in ('identity_queries', 'identity_tables', 'identity_constants',
                 'identity_guards', 'raw_versions', 'raw_rejections', 'manual_flags',
                 'retrieval', 'webhook_status', 'garmin', 'oauth_credentials', 'apple',
                 'identity'):
        importlib.import_module('app.models.' + name)
    importlib.reload(identity)
    assert tuple(Base.metadata.dispatch.after_create) == listeners
    assert set(Base.registry.mappers) == mappers
    assert dict(Base.metadata.tables) == tables

    names = (*identity_constants.GUARDED_TABLES, identity_constants.RECONCILE_TABLE,
             identity_constants.DEFERRAL_TABLE)
    assert len(names) == 11
    for name in names:
        mapped = [mapper for mapper in mappers if mapper.local_table.name == name]
        assert len(mapped) == 1, name
        cls = mapped[0].class_
        assert cls is getattr(identity_tables, cls.__name__)
        assert cls.__table__ is Base.metadata.tables[name]
    for declarations in (list(REGISTRY), list(reversed(REGISTRY))):
        REGISTRY[:] = declarations
        for dialect, ddl in (('postgresql', 'POSTGRESQL_DDL'), ('sqlite', 'SQLITE_DDL')):
            statements = []
            def capture(sql, *args, **kwargs):
                statements.append(str(sql.compile(dialect=engine.dialect)))
            engine = create_mock_engine(dialect + '://', capture)
            Base.metadata.create_all(engine, checkfirst=False)
            models = (raw_versions, identity_guards, manual_flags, raw_rejections,
                      retrieval, webhook_status, derived_invalidation, garmin,
                      oauth_credentials, heart_invalidation, apple)
            expected = tuple(sql for model in models for sql in (
                sqlite_guards.ddl(model) if dialect == 'sqlite' else model.POSTGRESQL_DDL))
            assert set(expected) <= set(statements)
            functions = [i for i, sql in enumerate(statements)
                         if sql.startswith('CREATE FUNCTION')]
            triggers = [i for i, sql in enumerate(statements)
                        if sql.startswith(('CREATE TRIGGER', 'CREATE CONSTRAINT TRIGGER'))]
            if dialect == 'postgresql':
                # + Apple's 4 (two append-only tables)
                assert len(triggers) - len(heart_invalidation.GUARDS.pg) == 105
                assert len(functions) - len(heart_invalidation.GUARDS.functions) == 27
                assert max(functions) < min(triggers)
            else:
                assert len(triggers) == 100  # + Apple's 4
            views = [i for i, sql in enumerate(statements) if sql.startswith('CREATE VIEW')]
            assert max(triggers) < min(views)
            counts = Counter(statements)
            for sql in expected:
                assert counts[sql] == 1, sql

    # Execute SQLite DDL too: duplicate trigger registration must fail create_all.
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        triggers = dict(connection.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        )).all())
    expected = sqlite_guards.definitions()
    assert triggers.keys() == expected.keys()
    for name, (_, sql) in expected.items():
        assert triggers[name] == sql
    engine.dispose()
    """
)


@pytest.mark.parametrize(
    "first",
    [
        "identity",
        "identity_constants",
        "identity_tables",
        "identity_queries",
        "identity_guards",
        "raw_versions",
        "raw_rejections",
        "manual_flags",
        "retrieval",
        "webhook_status",
        "garmin",
        "oauth_credentials",
        "apple",
        "heart_invalidation",
        "heart_projection",
    ],
)
def test_cold_imports_register_models_and_guards_once(first):
    # Use a fresh interpreter: conftest already imports app.models in the test process.
    result = subprocess.run(
        [sys.executable, "-c", COLD_IMPORT_SCRIPT, f"app.models.{first}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
