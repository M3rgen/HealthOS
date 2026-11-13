"""SQLite model DDL and registration; production imports never reach this module."""

from importlib import import_module

from sqlalchemy import DDL, BigInteger, Integer, event

from app.db import guards
from app.db.base import Base
from app.models import derived_invalidation as derived_model
from app.models import identity_guards as identity_model
from app.models import raw_versions as raw_model
from support.sqlite_guard_helpers import append_only

# The unit schema uses the migrations' frozen test DDL, rather than maintaining a second
# implementation. These revisions have no runtime dependencies on app.models.
SPECIAL = (
    (
        raw_model.GUARDS,
        import_module("migrations.versions.20261020_0011_raw_versions")._SQLITE_TRIGGERS,
    ),
    (
        identity_model.GUARDS,
        import_module("migrations.versions.20261021_0012_record_identity")._SQLITE_TRIGGERS,
    ),
    (
        derived_model.GUARDS,
        import_module("migrations.versions.20261025_0018_derived_days").SQLITE_TRIGGER_SQL,
    ),
)


def definitions(declaration=None):
    triggers = {}
    for owner in guards.REGISTRY if declaration is None else (declaration,):
        sql = next((sql for special, sql in SPECIAL if special is owner), None)
        sql = append_only(owner) if sql is None else sql
        if duplicate := triggers.keys() & sql.keys():
            raise ValueError(f"Duplicate archive guard definitions: {sorted(duplicate)}")
        triggers.update(sql)
    return triggers


def ddl(model):
    return (*(sql for _, sql in definitions(model.GUARDS).values()), *model.GUARDS.extra)


SQLITE_TRIGGER_SQL = definitions()


def create_guards(target, connection, **kwargs):
    triggers = definitions()
    statements = (
        *(sql for _, sql in triggers.values()),
        *(sql for declaration in guards.REGISTRY for sql in declaration.extra),
    )
    for statement in statements:
        DDL(statement).execute_if(dialect="sqlite")(target, connection, **kwargs)


def install():
    """Adapt only the test schema; PostgreSQL DDL stays identical before and after this call."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if (
                isinstance(column.type, BigInteger)
                and "sqlite" not in column.type._variant_mapping
                and (
                    (column.primary_key and column.autoincrement is True)
                    or column.foreign_keys
                    or (table.name, column.name)
                    in {
                        ("raw_health_data_rejections", "response_id"),
                        ("raw_rejection_acks", "through_id"),
                        ("retrieval_event_rejections", "event_id"),
                    }
                )
            ):
                column.type = column.type.with_variant(Integer(), "sqlite")
        for index in table.indexes:
            if (where := index.dialect_options["postgresql"].get("where")) is not None:
                index.dialect_options["sqlite"]["where"] = where
    if not event.contains(Base.metadata, "after_create", create_guards):
        event.listen(Base.metadata, "after_create", create_guards)
