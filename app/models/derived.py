"""Disposable, versioned local read models; never a replacement for the archive."""

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Date, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DerivedDay(Base):
    __tablename__ = "derived_days"
    __table_args__ = (Index("ix_derived_days_pending", "built_revision", "revision"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Real UTC bounds, so triggers invalidate every registered timezone, including DST.
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_stop: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    civil_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    civil_stop: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    built_revision: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    build_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DerivedDayPending(Base):
    """Days a transaction dirtied, written by the row triggers and drained at its commit.

    PostgreSQL only (app/models/derived_invalidation.py); rows never outlive the writing
    transaction, so nothing here is ever visible to another one.
    """

    __tablename__ = "derived_day_pending"

    xact: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(128), primary_key=True)
    method: Mapped[str] = mapped_column(String(64), primary_key=True)
