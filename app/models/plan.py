"""Private plan versions are immutable; the singleton holds replaceable UI state."""

from datetime import date, datetime

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.guards import GuardSet


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (CheckConstraint("schema_version = 1", name="schema_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)


class PlanState(TimestampMixin, Base):
    __tablename__ = "plan_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("schema_version = 1", name="schema_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    checked_item_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)


TABLES = ("plan_versions", "plan_state")

GUARDS = GuardSet()
GUARDS.append_only("plan_versions")

POSTGRESQL_DDL = GUARDS.postgresql_ddl
