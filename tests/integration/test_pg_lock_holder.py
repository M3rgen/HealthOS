"""The refusal message names the holder of the lock in this database, not in another one.

Advisory locks belong to one database, but ``pg_locks`` shows the whole cluster: on a server
holding several databases the query must not report a process from a neighbouring one.
"""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


async def test_a_holder_in_another_database_is_not_named(pg_sessions, database_url):
    from app.services.identities.source_chain_locks import BACKFILL_LOCK, _lock_holder

    url = make_url(database_url).set(drivername="postgresql")
    own = url.render_as_string(hide_password=False)
    other_name = url.database + "_other"
    other = url.set(database=other_name).render_as_string(hide_password=False)
    with psycopg.connect(own, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{other_name}"')
    try:
        name = "healthos-identity-backfill"
        with psycopg.connect(other, autocommit=True, application_name=name) as holder:
            holder.execute("SELECT pg_advisory_lock(%s, %s)", BACKFILL_LOCK)
            async with pg_sessions() as session:
                connection = await session.connection()
                message = await _lock_holder(connection)
    finally:
        with psycopg.connect(own, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{other_name}" WITH (FORCE)')
    assert message == "another identity-backfill or identity-decide"
