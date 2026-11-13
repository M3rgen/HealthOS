"""Native credential serialization and immutable account-change evidence."""

import asyncio

import pytest
from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.models.oauth_credentials import OAuthCredentialVersion
from app.services.providers.credentials import CredentialStore, GoogleAccountConflict

pytestmark = pytest.mark.integration


async def test_concurrent_first_connections_cannot_silently_switch_accounts(pg_sessions):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    first, second = CredentialStore(settings, pg_sessions), CredentialStore(settings, pg_sessions)
    results = await asyncio.gather(
        first.save_connection("synthetic-first", None, "first", None),
        second.save_connection("synthetic-second", None, "second", None),
        return_exceptions=True,
    )
    assert results.count(None) == 1
    assert sum(isinstance(result, GoogleAccountConflict) for result in results) == 1
    async with pg_sessions() as session:
        assert len((await session.scalars(select(OAuthCredentialVersion))).all()) == 1


def test_event_guards_and_downgrade_keep_history(database_url, migrated, alembic_config):
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        with engine.begin() as connection:
            identifier = connection.scalar(
                text(
                    "INSERT INTO oauth_credential_versions (subject, encrypted_refresh_token) "
                    "VALUES ('me', 'synthetic') RETURNING id"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO google_account_rebind_events "
                    "(credential_version_id, old_health_user_id, new_health_user_id) "
                    "VALUES (:id, 'old', 'new')"
                ),
                {"id": identifier},
            )
        for statement in (
            "UPDATE google_account_rebind_events SET new_health_user_id = 'changed'",
            "DELETE FROM google_account_rebind_events",
            "TRUNCATE google_account_rebind_events",
        ):
            with pytest.raises(DBAPIError, match="refused"), engine.begin() as connection:
                connection.execute(text(statement))
        with pytest.raises(RuntimeError, match="history; downgrade refused"):
            command.downgrade(alembic_config, "20261108_0026")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM google_account_rebind_events")) == 1
    finally:
        engine.dispose()
