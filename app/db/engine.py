"""One bounded pool for requests, ownership and page transactions."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.db.dialect import for_dialect

# Session settings sent when asyncpg opens a connection (no statement per transaction).
# JIT off: the readers' per-row eligibility subqueries push planner cost estimates past
# jit_above_cost and jit_optimize_above_cost although they read a few thousand rows; on the
# two-core server each such statement spent 0.6-1.0 s compiling to run for milliseconds.
ASYNCPG_SERVER_SETTINGS = {"jit": "off"}


def create_shared_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url)
    options = for_dialect(url.get_backend_name()).pool_options(
        settings.sync_concurrency, settings.garmin_enabled, url=url
    )
    if url.get_driver_name() == "asyncpg":
        options["connect_args"] = {"server_settings": dict(ASYNCPG_SERVER_SETTINGS)}
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        # Bound values (raw records, source metadata) stay out of SQLAlchemy error messages.
        hide_parameters=True,
        execution_options={"ownership_concurrency": settings.sync_concurrency},
        **options,
    )
