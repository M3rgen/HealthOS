from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401
from app.core.config import get_settings
from app.db.base import Base
from app.db.downgrade_guard import install as install_downgrade_guard
from app.db.downgrade_guard import refuse_lossy_plan
from app.db.migration_compatibility import resolve_legacy_placeholders

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
config.set_main_option(
    "sqlalchemy.url", get_settings().database_url.replace("+asyncpg", "+psycopg")
)
target_metadata = Base.metadata
# A downgrade that would lose rows refuses here, for every revision (no migration is edited).
install_downgrade_guard()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            resolve_legacy_placeholders(context.get_context())
            refuse_lossy_plan(context.get_context())
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
