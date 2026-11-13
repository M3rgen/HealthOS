"""Rotation requires consent without rewriting historical ciphertext or using stale workers."""

import asyncio

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.oauth_keys import GoogleReauthorizationRequired, token_cipher
from app.google_health.oauth import AccessToken
from app.models.oauth_credentials import OAuthCredentialVersion, current_credential
from app.runtime import build_runtime
from app.services.providers.credentials import CredentialStore, CredentialStoreError


async def test_environment_bootstrap_is_bound_to_root_and_cannot_bypass_consent(sessions):
    settings = Settings(
        _env_file=None,
        google_refresh_token="synthetic-bootstrap",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )
    store = CredentialStore(settings, sessions)
    assert await store.load_refresh_token() == "synthetic-bootstrap"
    assert await store.load_refresh_token() == "synthetic-bootstrap"
    assert await store.account_is_current()
    settings.google_token_encryption_key = Fernet.generate_key().decode()
    assert not await store.account_is_current()
    with pytest.raises(GoogleReauthorizationRequired):
        await CredentialStore(settings, sessions).load_refresh_token()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OAuthCredentialVersion)) == 1


@pytest.mark.parametrize("inflight", [False, True])
async def test_same_account_consent_fences_cached_and_inflight_tokens(
    sessions, monkeypatch, inflight
):
    old = Fernet.generate_key().decode()
    old_settings = Settings(_env_file=None, google_token_encryption_key=old)
    settings = Settings(
        _env_file=None,
        google_token_encryption_key=Fernet.generate_key().decode(),
        google_token_encryption_keys_old=[old],
    )
    runtime = build_runtime(old_settings, sessions)
    fresh = CredentialStore(settings, sessions)
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(token):
        assert token == "synthetic-retired"
        entered.set()
        if inflight:
            await release.wait()
        return AccessToken("synthetic-retired-access", 99999999999, "synthetic-retired-replacement")

    monkeypatch.setattr(runtime.oauth, "refresh", refresh)
    try:
        await runtime.credentials.save_connection("synthetic-retired", "scope", "account", "legacy")
        pending = asyncio.create_task(runtime.tokens.get())
        await asyncio.wait_for(entered.wait(), 30)
        if not inflight:
            assert await asyncio.wait_for(pending, 30) == "synthetic-retired-access"
        await fresh.save_connection("synthetic-consented", None, "account", "legacy")
        assert not await runtime.credentials.account_is_current()
        with pytest.raises(CredentialStoreError, match="connection changed"):
            await runtime.credentials.save_refresh_token("synthetic-stale")
        # Even a fresh stale-root worker without a loaded snapshot cannot append.
        with pytest.raises(GoogleReauthorizationRequired):
            await CredentialStore(old_settings, sessions).save_refresh_token("synthetic-stale")
        release.set()
        with pytest.raises(GoogleReauthorizationRequired):
            if inflight:
                await asyncio.wait_for(pending, 30)
            else:
                await runtime.tokens.get()
        async with sessions() as session:
            row = await current_credential(session)
            encrypted = row.encrypted_refresh_token.encode()
            assert (row.health_user_id, row.legacy_user_id, row.scopes) == (
                "account",
                "legacy",
                "scope",
            )
            assert token_cipher(settings).decrypt(encrypted) == b"synthetic-consented"
            with pytest.raises(InvalidToken):
                token_cipher(old_settings).decrypt(encrypted)
            with pytest.raises(InvalidToken):
                Fernet(old.encode()).decrypt(encrypted)
        assert await fresh.load_refresh_token() == "synthetic-consented"
        await fresh.save_refresh_token("synthetic-replacement")
        settings.google_token_encryption_keys_old = []
        assert (
            await CredentialStore(settings, sessions).load_refresh_token()
            == "synthetic-replacement"
        )
    finally:
        release.set()
        await runtime.shutdown()


async def test_retired_configuration_cannot_affect_current_root_credentials(sessions):
    settings = Settings(
        _env_file=None,
        google_token_encryption_key=Fernet.generate_key().decode(),
        google_token_encryption_keys_old=["unusable-retired-root"],
    )
    store = CredentialStore(settings, sessions)
    await store.save_connection("synthetic-current", None, "account", None)
    assert await store.load_refresh_token() == "synthetic-current"
