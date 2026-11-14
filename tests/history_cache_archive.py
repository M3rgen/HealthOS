"""Bounded synthetic minute archives and counts of rows delivered to history readers."""

from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from derived_records import steps
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import AsyncResult, AsyncScalarResult, AsyncSession, async_sessionmaker
from support.postgres_reads import read_statements

from app.models.health import Steps
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard_history import history_data

TODAY = date(2026, 9, 30)
REVISION_READS = {
    "SELECT data_version, total_changes() FROM pragma_data_version",
    "SELECT pg_current_snapshot()::text, pg_current_xact_id_if_assigned()::text",
}


@asynccontextmanager
async def stable_history_snapshot(sessions):
    """Export a stable PG snapshot to independent request transactions despite xdist writers."""
    if sessions.kw["bind"].dialect.name != "postgresql":
        yield sessions
        return
    async with sessions.kw["bind"].connect() as connection:
        await connection.execution_options(isolation_level="REPEATABLE READ")
        async with connection.begin():
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            snapshot = await connection.scalar(text("SELECT pg_export_snapshot()"))
            yield async_sessionmaker(
                sessions.kw["bind"], expire_on_commit=False, info={"history_snapshot": snapshot}
            )


async def seed_minutes(sessions, days, density=1000):
    """At most one day's bodies in memory; no personal data or upstream connection."""
    async with sessions.begin() as session:
        for offset in range(days):
            start = datetime.combine(TODAY - timedelta(days=offset + 31), datetime.min.time(), UTC)
            raw, normalized = [], []
            for minute in range(density):
                stamp = start + timedelta(minutes=minute)
                stop = stamp + timedelta(minutes=1)
                key = f"synthetic/steps/{offset}-{minute}"
                raw.append(
                    dict(
                        id=uuid4(),
                        google_data_point_id=key,
                        data_type="steps",
                        raw_json=steps(f"{offset}-{minute}", count=1, start=stamp, end=stop),
                        payload_hash="0" * 64,
                        source_start_time=stamp,
                        source_end_time=stop,
                        first_seen_at=start,
                        last_seen_at=start,
                    )
                )
                normalized.append(
                    dict(google_data_point_id=key, start_time=stamp, end_time=stop, count=1)
                )
            await session.execute(insert(RawHealthData), raw)
            await session.execute(insert(Steps), normalized)


@contextmanager
def counted_reads(sessions, monkeypatch):
    """Count delivered rows and SQL statements, excluding exact PG controls.

    The archive is outside page one, so an unchanged page must read zero raw payloads.
    Streaming counts operate at SQLAlchemy's cursor consumer on both database backends.
    Zero tags count buffered probe rows; ones count streamed archive rows.
    """
    statements, rows = [], []
    engine = sessions.kw["bind"].sync_engine

    def trace(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    with monkeypatch.context() as patch:
        execute = AsyncSession.execute

        async def counted_execute(self, *args, **kwargs):
            result = await execute(self, *args, **kwargs)
            # GitHub-only Apple extension: its readers run ORM selects, whose result has no
            # ``returns_rows`` (it always carries rows); freezing counts them the same way.
            if getattr(result, "returns_rows", True):
                frozen = result.freeze()
                rows.extend([0] * len(frozen.data))
                return frozen()
            return result

        patch.setattr(AsyncSession, "execute", counted_execute)
        for cls in (AsyncResult, AsyncScalarResult):
            original = cls.__anext__

            async def tracked(self, *, next_row=original):
                row = await next_row(self)
                rows.append(1)
                return row

            patch.setattr(cls, "__anext__", tracked)
        event.listen(engine, "before_cursor_execute", trace)
        try:
            yield statements, rows
        finally:
            event.remove(engine, "before_cursor_execute", trace)


async def measured_page(sessions, monkeypatch, **options):
    async with sessions() as session:
        if snapshot := session.info.get("history_snapshot"):
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            # Only PostgreSQL's own exported identifier, never an external input.
            await session.execute(text(f"SET TRANSACTION SNAPSHOT '{snapshot}'"))
        with counted_reads(sessions, monkeypatch) as (statements, rows):
            page = await history_data(session, "steps", 1, "UTC", TODAY, **options)
    return page, (len(read_statements(statements)), len(rows), sum(rows))
