"""Self-registering archive guards, installed after all tables exist.

Model modules declare a GuardSet. Installation creates every PostgreSQL function before
any trigger, so shared functions never depend on model import order. Frozen migrations
remain independent of these declarations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import DDL, event
from sqlalchemy.engine import Connection

from app.db.base import Base


@dataclass
class GuardSet:
    pg: dict[str, tuple[str, str]] = field(default_factory=dict)
    immutable_tables: tuple[str, ...] = ()
    functions: dict[str, str] = field(default_factory=dict)
    triggers: tuple[tuple[str, str, str], ...] = ()
    extra: tuple[str, ...] = ()
    function_separator: str = "\n"

    def __post_init__(self) -> None:
        REGISTRY.append(self)

    def postgresql(self, name: str, table: str, definition: str, function: str) -> None:
        self.pg[name] = (
            table,
            f"CREATE TRIGGER {name}\n{definition}\nEXECUTE FUNCTION {function}()",
        )
        self.triggers += ((table, name, function),)

    def append_only(self, table: str) -> None:
        """Refuse updates/deletes and PostgreSQL truncation, also in replica sessions."""
        function = "healthos_refuse_change"
        for suffix, operations, level in (
            ("append_only", "UPDATE OR DELETE", "ROW"),
            ("no_truncate", "TRUNCATE", "STATEMENT"),
        ):
            name = f"{table}_{suffix}"
            self.postgresql(
                name, table, f"BEFORE {operations} ON {table} FOR EACH {level}", function
            )
        self.immutable_tables += (table,)

    @property
    def function_ddl(self) -> tuple[str, ...]:
        return tuple(
            f"CREATE FUNCTION {name}() RETURNS trigger{self.function_separator}"
            f"LANGUAGE plpgsql AS $${body}$$"
            for name, body in self.functions.items()
        )

    @property
    def postgresql_ddl(self) -> tuple[str, ...]:
        return (
            *self.function_ddl,
            *(sql for _, sql in self.pg.values()),
            *(
                f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"
                for table, name, _ in self.triggers
            ),
            *self.extra,
        )


REGISTRY: list[GuardSet] = []


def definitions(declaration: GuardSet | None = None):
    """Collect all definitions or one declaration; reject duplicates before any DDL."""
    pg, functions = {}, {}
    triggers = []
    for guards in REGISTRY if declaration is None else (declaration,):
        for target, source in (
            (pg, guards.pg),
            (functions, guards.functions),
        ):
            if duplicate := target.keys() & source.keys():
                raise ValueError(f"Duplicate archive guard definitions: {sorted(duplicate)}")
            target.update(source)
        triggers.extend(guards.triggers)
    return pg, functions, tuple(triggers)


def create_guards(target, connection: Connection, **kwargs) -> None:
    pg, _, triggers = definitions()
    postgresql = (
        *(sql for guards in REGISTRY for sql in guards.function_ddl),
        *(sql for _, sql in pg.values()),
        *(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}" for table, name, _ in triggers),
    )
    extra = tuple(sql for guards in REGISTRY for sql in guards.extra)
    for statement in (*postgresql, *extra):
        DDL(statement).execute_if(dialect="postgresql")(target, connection, **kwargs)


event.listen(Base.metadata, "after_create", create_guards)
