"""Append-only, value-free webhook diagnostic observations.

Guards refuse UPDATE/DELETE and PostgreSQL TRUNCATE, including replica sessions.
Migration 20261025_0019 freezes the same SQL for existing databases.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.guards import GuardSet

TABLE = "webhook_status_events"


class WebhookStatusEvent(Base):
    __tablename__ = TABLE
    __table_args__ = (Index("ix_webhook_status_reason_time", "reason", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reason: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


GUARDS = GuardSet()
for _table in (TABLE,):
    GUARDS.append_only(_table)

FUNCTION_BODIES = GUARDS.functions
POSTGRESQL_DDL = GUARDS.postgresql_ddl
