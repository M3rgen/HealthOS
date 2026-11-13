"""Append-only credential versions; oauth_tokens remains the untouched legacy fallback."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.guards import GuardSet
from app.models.state import OAuthToken


class OAuthCredentialVersion(Base):
    __tablename__ = "oauth_credential_versions"
    __table_args__ = (Index("ix_oauth_credential_versions_subject_id", "subject", "id"),)

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(128), nullable=False, default="me")
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str | None] = mapped_column(Text)
    health_user_id: Mapped[str | None] = mapped_column(String(128))
    legacy_user_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GoogleAccountRebindEvent(Base):
    __tablename__ = "google_account_rebind_events"

    id: Mapped[int] = mapped_column(BigInteger(), primary_key=True, autoincrement=True)
    credential_version_id: Mapped[int] = mapped_column(
        ForeignKey("oauth_credential_versions.id"), nullable=False, unique=True
    )
    old_health_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    new_health_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


async def current_credential(
    session: AsyncSession, subject: str = "me"
) -> OAuthCredentialVersion | OAuthToken | None:
    version = await session.scalar(
        select(OAuthCredentialVersion)
        .where(OAuthCredentialVersion.subject == subject)
        .order_by(OAuthCredentialVersion.id.desc())
        .limit(1)
    )
    return version if version is not None else await session.get(OAuthToken, subject)


TABLE = OAuthCredentialVersion.__tablename__
REBIND_TABLE = GoogleAccountRebindEvent.__tablename__
TABLES = (TABLE, REBIND_TABLE)
GUARDS = GuardSet()
for _table in TABLES:
    GUARDS.append_only(_table)

FUNCTION_BODIES = GUARDS.functions
POSTGRESQL_DDL = GUARDS.postgresql_ddl
