"""The single-run refusal names this database's lock holder, never another database's."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy.engine import make_url

from app.services.identities.source_chain_locks import BACKFILL_LOCK, _lock_holder

pytestmark = pytest.mark.integration


def _dsn(url, database):
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


async def test_a_holder_in_another_database_is_not_this_archives_holder(pg_sessions, database_url):
    """Parallel test databases (or a restore on the same server) hold the same advisory key."""
    other = f"healthos_it_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_dsn(database_url, "postgres"), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{other}"')
    try:
        with psycopg.connect(
            _dsn(database_url, other),
            autocommit=True,
            application_name="healthos-identity-backfill",
        ) as elsewhere:
            elsewhere.execute("SELECT pg_advisory_lock(%s, %s)", BACKFILL_LOCK)
            async with pg_sessions.kw["bind"].connect() as connection:
                message = await _lock_holder(connection)
            assert message == "another identity-backfill or identity-decide"
    finally:
        with psycopg.connect(_dsn(database_url, "postgres"), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{other}" WITH (FORCE)')
