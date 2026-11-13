from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.oauth_keys import CredentialStoreError as CredentialStoreError
from app.core.oauth_keys import GoogleReauthorizationRequired, token_cipher
from app.db.dialect import database
from app.db.postgres import CREDENTIAL_LOCK_NAMESPACE
from app.models.oauth_credentials import (
    GoogleAccountRebindEvent,
    OAuthCredentialVersion,
    current_credential,
)
from app.models.state import OAuthToken


class GoogleAccountConflict(CredentialStoreError):
    def __init__(self):
        super().__init__("Bu arşiv başka bir Google hesabına bağlı.")


class CredentialStore:
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings = settings
        self.sessions = sessions
        self._lock = asyncio.Lock()
        self._loaded_connection: tuple[str | None, str] | None = None

    @asynccontextmanager
    async def _write(self):
        # Also serialize independent API/CLI processes, including the first connection
        # where there is no row to lock yet. No Google I/O runs in this transaction.
        async with self._lock, self.sessions.begin() as session:
            await database(session).credential_lock(session, CREDENTIAL_LOCK_NAMESPACE)
            yield session

    async def check_account(self, health_user_id: str) -> None:
        """Reject account changes before loading or activating any credential."""
        async with self._lock, self.sessions.begin() as session:
            token = await current_credential(session)
            self._check_account(token, health_user_id)

    async def account_is_current(self) -> bool:
        """Fence cached/in-flight tokens across account, consent and root changes."""
        async with self._lock, self.sessions.begin() as session:
            token = await current_credential(session)
            if token is None or self._loaded_connection != self._connection(token):
                return False
            try:
                self._decrypt(token)
            except CredentialStoreError:
                return False
            return True

    @staticmethod
    def _connection(token: OAuthCredentialVersion | OAuthToken) -> tuple[str | None, str]:
        return token.health_user_id, token.encrypted_refresh_token

    def _decrypt(self, token: OAuthCredentialVersion | OAuthToken) -> str:
        try:
            return (
                token_cipher(self.settings).decrypt(token.encrypted_refresh_token.encode()).decode()
            )
        except (InvalidToken, UnicodeError):
            raise GoogleReauthorizationRequired() from None

    @staticmethod
    def _check_account(
        token: OAuthCredentialVersion | OAuthToken | None, health_user_id: str
    ) -> None:
        if token and token.health_user_id and token.health_user_id != health_user_id:
            raise GoogleAccountConflict()

    async def load_refresh_token(self) -> str | None:
        async with self._write() as session:
            token = await current_credential(session)
            if token is None:
                if not self.settings.google_refresh_token:
                    return None
                # Bind initial offline configuration to this root, even when Google
                # never returns a replacement token. It cannot bypass later rotation.
                token = OAuthCredentialVersion(
                    subject="me",
                    encrypted_refresh_token=token_cipher(self.settings)
                    .encrypt(self.settings.google_refresh_token.encode())
                    .decode(),
                )
                session.add(token)
            refresh_token = self._decrypt(token)
            connection = self._connection(token)
        self._loaded_connection = connection
        return refresh_token

    async def save_refresh_token(self, refresh_token: str, scopes: str | None = None) -> None:
        primary = token_cipher(self.settings)
        encrypted = primary.encrypt(refresh_token.encode()).decode()
        async with self._write() as session:
            token = await current_credential(session)
            if self._loaded_connection is not None and (
                token is None or self._loaded_connection != self._connection(token)
            ):
                raise CredentialStoreError("Google connection changed; retry token refresh")
            if token is not None:
                # A background refresh cannot establish consent under a new root.
                self._decrypt(token)
            session.add(
                OAuthCredentialVersion(
                    subject="me",
                    encrypted_refresh_token=encrypted,
                    scopes=scopes if scopes is not None else token.scopes if token else None,
                    health_user_id=token.health_user_id if token else None,
                    legacy_user_id=token.legacy_user_id if token else None,
                )
            )
            connection = (token.health_user_id if token else None, encrypted)
        self._loaded_connection = connection

    async def save_connection(
        self,
        refresh_token: str,
        scopes: str | None,
        health_user_id: str,
        legacy_user_id: str | None,
        *,
        require_current: bool = False,
    ) -> None:
        """Activate a verified token and its identity in one transaction."""
        await self._save_connection(
            refresh_token, scopes, health_user_id, legacy_user_id, require_current=require_current
        )

    async def rebind_connection(
        self,
        refresh_token: str,
        scopes: str | None,
        health_user_id: str,
        legacy_user_id: str | None,
    ) -> None:
        """CLI-only deliberate account change; retain both credentials and its event."""
        await self._save_connection(
            refresh_token, scopes, health_user_id, legacy_user_id, rebind=True
        )

    async def _save_connection(
        self,
        refresh_token: str,
        scopes: str | None,
        health_user_id: str,
        legacy_user_id: str | None,
        *,
        rebind: bool = False,
        require_current: bool = False,
    ) -> None:
        primary = token_cipher(self.settings)
        encrypted = primary.encrypt(refresh_token.encode()).decode()
        async with self._write() as session:
            token = await current_credential(session)
            if not rebind:
                self._check_account(token, health_user_id)
            if require_current:
                # Callback reuse includes Google I/O after loading. Consent or a key
                # change during that wait must not reactivate the earlier token.
                if token is None or self._loaded_connection != self._connection(token):
                    raise GoogleReauthorizationRequired()
                self._decrypt(token)
            if scopes is None and token and (not rebind or token.health_user_id == health_user_id):
                scopes = token.scopes
            version = OAuthCredentialVersion(
                subject="me",
                encrypted_refresh_token=encrypted,
                scopes=scopes,
                health_user_id=health_user_id,
                legacy_user_id=legacy_user_id,
            )
            session.add(version)
            if rebind and token and token.health_user_id and token.health_user_id != health_user_id:
                await session.flush()
                session.add(
                    GoogleAccountRebindEvent(
                        credential_version_id=version.id,
                        old_health_user_id=token.health_user_id,
                        new_health_user_id=health_user_id,
                    )
                )
        self._loaded_connection = (health_user_id, encrypted)
