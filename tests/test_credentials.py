import base64

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.oauth_keys import GoogleReauthorizationRequired, derive_key, token_cipher
from app.models.oauth_credentials import OAuthCredentialVersion, current_credential
from app.models.state import OAuthToken
from app.services.providers.credentials import CredentialStore, CredentialStoreError


async def test_rotated_database_token_precedes_stale_environment_fallback(sessions):
    settings = Settings(
        google_refresh_token="environment-fallback",
        google_token_encryption_key=Fernet.generate_key().decode(),
    )
    store = CredentialStore(settings, sessions)

    await store.save_refresh_token("initial", "scope-a scope-b")
    async with sessions() as session:
        first = (await current_credential(session)).encrypted_refresh_token
    await store.save_refresh_token("rotated")

    assert await store.load_refresh_token() == "rotated"
    async with sessions() as session:
        token = await current_credential(session)
        rows = (
            await session.scalars(
                select(OAuthCredentialVersion).order_by(OAuthCredentialVersion.id)
            )
        ).all()
        assert len(rows) == 2 and rows[0].encrypted_refresh_token == first
    assert token.scopes == "scope-a scope-b"


async def test_environment_refresh_token_is_used_before_database_onboarding(sessions):
    store = CredentialStore(
        Settings(
            _env_file=None,
            google_refresh_token="environment-only",
            google_token_encryption_key=Fernet.generate_key().decode(),
        ),
        sessions,
    )

    assert await store.load_refresh_token() == "environment-only"


async def test_verified_connection_creates_encrypted_token_and_identity_together(sessions):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    store = CredentialStore(settings, sessions)
    await store.save_connection("offline", "scope", "account", "legacy")
    async with sessions() as session:
        row = await current_credential(session)
        assert row.encrypted_refresh_token != "offline"
        assert (row.scopes, row.health_user_id, row.legacy_user_id) == (
            "scope",
            "account",
            "legacy",
        )
    assert await store.load_refresh_token() == "offline"


@pytest.mark.parametrize("format", ["legacy", "v1", "v1-already-rewrapped", "v2"])
async def test_retired_credentials_require_consent_without_changing_history(
    sessions, monkeypatch, format
):
    old, current = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    old_settings = Settings(_env_file=None, google_token_encryption_key=old)
    cipher = {
        "legacy": Fernet(old.encode()),
        "v1": Fernet(base64.urlsafe_b64encode(derive_key(old, b"fernet"))),
        "v1-already-rewrapped": Fernet(base64.urlsafe_b64encode(derive_key(current, b"fernet"))),
        "v2": token_cipher(old_settings),
    }[format]
    encrypted = cipher.encrypt(b"synthetic-refresh").decode()
    model = OAuthToken if format == "legacy" else OAuthCredentialVersion
    async with sessions.begin() as session:
        if format == "v1-already-rewrapped":
            session.add(
                OAuthToken(
                    subject="me",
                    encrypted_refresh_token=Fernet(old.encode())
                    .encrypt(b"synthetic-refresh")
                    .decode(),
                )
            )
        session.add(
            model(
                subject="me",
                encrypted_refresh_token=encrypted,
                scopes="scope",
                health_user_id="synthetic-account",
                legacy_user_id="synthetic-legacy",
            )
        )
    # Exercise the actual optional CSV setting, including whitespace/multiple roots.
    monkeypatch.setenv(
        "GOOGLE_TOKEN_ENCRYPTION_KEYS_OLD", f" {Fernet.generate_key().decode()}, {old} "
    )
    settings = Settings(_env_file=None, google_token_encryption_key=current)
    store = CredentialStore(settings, sessions)
    for operation in (store.load_refresh_token(), store.save_refresh_token("synthetic-refresh")):
        with pytest.raises(GoogleReauthorizationRequired, match="yeniden onay"):
            await operation
    async with sessions() as session:
        original = await session.scalar(
            select(model).where(model.encrypted_refresh_token == encrypted)
        )
        assert original.encrypted_refresh_token == encrypted
        assert (await current_credential(session)).encrypted_refresh_token == encrypted
    # Only explicit activation of a newly consented offline token restores access.
    await store.save_connection("new-consent", None, "synthetic-account", "synthetic-legacy")
    assert await store.load_refresh_token() == "new-consent"
    async with sessions() as session:
        latest = await current_credential(session)
        assert (
            await session.scalar(select(model).where(model.encrypted_refresh_token == encrypted))
        ).encrypted_refresh_token == encrypted
        assert (latest.scopes, latest.health_user_id, latest.legacy_user_id) == (
            "scope",
            "synthetic-account",
            "synthetic-legacy",
        )
        assert (
            token_cipher(settings).decrypt(latest.encrypted_refresh_token.encode())
            == b"new-consent"
        )
        with pytest.raises(InvalidToken):
            cipher.decrypt(latest.encrypted_refresh_token.encode())
        count = await session.scalar(select(func.count()).select_from(OAuthCredentialVersion))
    # A second read needs no old root and must not append another version.
    settings.google_token_encryption_keys_old = []
    assert await store.load_refresh_token() == "new-consent"
    async with sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(OAuthCredentialVersion)) == count
        )


async def test_legacy_cipher_under_current_root_also_requires_consent(sessions):
    root = Fernet.generate_key().decode()
    encrypted = Fernet(root.encode()).encrypt(b"synthetic-refresh").decode()
    async with sessions.begin() as session:
        session.add(OAuthToken(subject="me", encrypted_refresh_token=encrypted))
    store = CredentialStore(Settings(_env_file=None, google_token_encryption_key=root), sessions)
    with pytest.raises(GoogleReauthorizationRequired):
        await store.load_refresh_token()


def test_hkdf_separates_token_and_state_keys():
    root = Fernet.generate_key().decode()
    token_key = derive_key(root, b"fernet-reauthorized/v2")
    assert token_key == derive_key(root, b"fernet-reauthorized/v2")
    assert token_key != derive_key(root, b"fernet")
    assert token_key != derive_key(root, b"oauth-state-hmac")
    assert token_key != base64.urlsafe_b64decode(root)
    settings = Settings(_env_file=None, google_token_encryption_key=root)
    encrypted = token_cipher(settings).encrypt(b"synthetic-refresh")
    with pytest.raises(InvalidToken):
        Fernet(root.encode()).decrypt(encrypted)


async def test_malformed_primary_key_raises_credential_store_error(sessions):
    settings = Settings(
        _env_file=None,
        google_token_encryption_key="invalid-key",
    )
    with pytest.raises(CredentialStoreError, match="valid Fernet key"):
        await CredentialStore(settings, sessions).save_refresh_token("synthetic-refresh")


async def test_invalid_token_raises_credential_store_error_and_preserves_row(sessions):
    settings = Settings(_env_file=None, google_token_encryption_key=Fernet.generate_key().decode())
    async with sessions.begin() as session:
        session.add(OAuthToken(subject="me", encrypted_refresh_token="invalid-ciphertext"))
    with pytest.raises(GoogleReauthorizationRequired):
        await CredentialStore(settings, sessions).load_refresh_token()
    async with sessions() as session:
        assert (await current_credential(session)).encrypted_refresh_token == "invalid-ciphertext"
        assert await session.scalar(select(func.count()).select_from(OAuthCredentialVersion)) == 0
