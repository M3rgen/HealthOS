"""Explicit account changes preserve credentials and immutable old/new identity evidence."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cli_records import cli_database as cli_database
from cryptography.fernet import Fernet
from sqlalchemy import event, select
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import Settings
from app.google_health.oauth import AccessToken, TokenProvider
from app.models.oauth_credentials import (
    GoogleAccountRebindEvent,
    OAuthCredentialVersion,
    current_credential,
)
from app.models.state import OAuthToken
from app.runtime import build_runtime
from app.services.providers.credentials import (
    CredentialStore,
    CredentialStoreError,
    GoogleAccountConflict,
)


@pytest.mark.parametrize("legacy", [False, True])
async def test_shared_store_guard_preserves_legacy_and_versioned_accounts(sessions, legacy):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    store = CredentialStore(settings, sessions)
    model = OAuthToken if legacy else OAuthCredentialVersion
    async with sessions.begin() as session:
        session.add(model(subject="me", encrypted_refresh_token="synthetic", health_user_id="old"))
    with pytest.raises(GoogleAccountConflict):
        await store.save_connection("synthetic-new", None, "new", None)
    async with sessions() as session:
        rows = (await session.scalars(select(model))).all()
        assert len(rows) == 1 and rows[0].encrypted_refresh_token == "synthetic"
        assert (await current_credential(session)).health_user_id == "old"


def test_cli_requires_explicit_confirmation_before_prompt_or_runtime(monkeypatch):
    build = Mock()
    monkeypatch.setattr(cli, "build_runtime", build)
    result = CliRunner().invoke(cli.app, ["google-rebind"])
    assert result.exit_code == 2
    assert "--confirm-account-change" in result.stderr
    assert "token" not in result.stdout
    build.assert_not_called()


@pytest.mark.parametrize("failure", [None, "identity", "transport"])
def test_cli_verifies_candidate_and_preserves_history(cli_database, monkeypatch, failure):
    valid = failure is None
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    store = CredentialStore(settings, cli_database)
    asyncio.run(store.save_connection("synthetic-old", "old-scope", "old", "old-legacy"))
    oauth = SimpleNamespace(
        refresh=AsyncMock(return_value=AccessToken("candidate", 99999999999, "rotated", "scope"))
    )
    runtime = SimpleNamespace(
        oauth=oauth,
        google=SimpleNamespace(
            get_identity=AsyncMock(
                return_value={"healthUserId": "new"} if valid else {},
                side_effect=RuntimeError("synthetic-input") if failure == "transport" else None,
            )
        ),
        credentials=store,
        tokens=TokenProvider(oauth, store.load_refresh_token, store.save_refresh_token),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(cli, "build_runtime", lambda *_: runtime)
    result = CliRunner().invoke(
        cli.app, ["google-rebind", "--confirm-account-change"], input="synthetic-input\n"
    )
    assert result.exit_code == (0 if valid else 1), result.output
    assert "synthetic-input" not in result.output
    oauth.refresh.assert_awaited_once_with("synthetic-input")
    runtime.google.get_identity.assert_awaited_once_with(access_token="candidate")
    runtime.shutdown.assert_awaited_once()

    async def check():
        async with cli_database() as session:
            versions = (
                await session.scalars(
                    select(OAuthCredentialVersion).order_by(OAuthCredentialVersion.id)
                )
            ).all()
            events = (await session.scalars(select(GoogleAccountRebindEvent))).all()
            assert len(versions) == (2 if valid else 1)
            assert versions[0].health_user_id == "old"
            assert len(events) == int(valid)
            if valid:
                assert (events[0].old_health_user_id, events[0].new_health_user_id) == (
                    "old",
                    "new",
                )
                assert events[0].credential_version_id == versions[-1].id
                assert await store.load_refresh_token() == "rotated"
                with pytest.raises(GoogleAccountConflict):
                    await store.save_connection("synthetic-old", None, "old", None)
            else:
                assert await store.load_refresh_token() == "synthetic-old"

    asyncio.run(check())


async def test_event_failure_rolls_back_new_credential(sessions):
    store = CredentialStore(
        Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode()),
        sessions,
    )
    await store.save_connection("synthetic-old", None, "old", None)

    def fail_event(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO google_account_rebind_events"):
            raise RuntimeError("synthetic event failure")

    engine = sessions.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError, match="synthetic event failure"):
            await store.rebind_connection("synthetic-new", None, "new", None)
    finally:
        event.remove(engine, "before_cursor_execute", fail_event)
    async with sessions() as session:
        assert len((await session.scalars(select(OAuthCredentialVersion))).all()) == 1
        assert not (await session.scalars(select(GoogleAccountRebindEvent))).all()
    assert await store.load_refresh_token() == "synthetic-old"


async def test_refresh_started_before_another_runtime_rebind_cannot_relabel_old_token(sessions):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    first, second = CredentialStore(settings, sessions), CredentialStore(settings, sessions)
    await first.save_connection("synthetic-old", None, "old", None)
    assert await first.load_refresh_token() == "synthetic-old"
    await second.rebind_connection("synthetic-new", None, "new", None)
    with pytest.raises(CredentialStoreError, match="connection changed"):
        await first.save_refresh_token("synthetic-old-rotation")
    assert await second.load_refresh_token() == "synthetic-new"


def test_concurrent_first_connections_allow_only_one_account(cli_database):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())

    async def check():
        first, second = (
            CredentialStore(settings, cli_database),
            CredentialStore(settings, cli_database),
        )
        results = await asyncio.gather(
            first.save_connection("synthetic-first", None, "first", None),
            second.save_connection("synthetic-second", None, "second", None),
            return_exceptions=True,
        )
        assert sum(isinstance(result, GoogleAccountConflict) for result in results) == 1
        assert results.count(None) == 1
        async with cli_database() as session:
            assert len((await session.scalars(select(OAuthCredentialVersion))).all()) == 1

    asyncio.run(check())


@pytest.mark.parametrize("inflight", [False, True])
async def test_running_api_discards_old_access_tokens_after_cli_rebind(
    sessions, monkeypatch, inflight
):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    runtime = build_runtime(settings, sessions)
    other = CredentialStore(settings, sessions)
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(token):
        if token == "synthetic-old" and inflight:
            entered.set()
            await release.wait()
        return AccessToken(f"access-{token}", 99999999999)

    monkeypatch.setattr(runtime.oauth, "refresh", refresh)
    try:
        await runtime.credentials.save_connection("synthetic-old", None, "old", None)
        if inflight:
            pending = asyncio.create_task(runtime.tokens.get())
            await asyncio.wait_for(entered.wait(), 30)
        else:
            assert await runtime.tokens.get() == "access-synthetic-old"
        await other.rebind_connection("synthetic-new", None, "new", None)
        release.set()
        if inflight:
            assert await asyncio.wait_for(pending, 30) == "access-synthetic-new"
        assert await runtime.tokens.get() == "access-synthetic-new"
    finally:
        release.set()
        await runtime.shutdown()
