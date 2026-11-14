"""Disposable UTC-hour packets of validated original Google heart samples."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HeartHour(Base):
    __tablename__ = "google_heart_hours"

    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    built_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    method: Mapped[str | None] = mapped_column(String(64))
    samples: Mapped[list | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    digest: Mapped[str | None] = mapped_column(String(64))


class HeartProjectionState(Base):
    __tablename__ = "google_heart_projection_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_hour: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class HeartPending(Base):
    """Hours a transaction already bumped, written by the heart row triggers.

    PostgreSQL only (app/models/heart_invalidation.py): it dedupes one transaction's bumps,
    and a deferred trigger deletes its rows at commit, so no other transaction sees one.
    """

    __tablename__ = "google_heart_pending"

    xact: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
