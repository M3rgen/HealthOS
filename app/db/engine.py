"""One bounded pool for requests, ownership and page transactions."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.db.dialect import for_dialect


def create_shared_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url)
    options = for_dialect(url.get_backend_name()).pool_options(
        settings.sync_concurrency, settings.garmin_enabled, url=url
    )
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        # Bound values (raw records, source metadata) stay out of SQLAlchemy error messages.
        hide_parameters=True,
        execution_options={"ownership_concurrency": settings.sync_concurrency},
        **options,
    )
