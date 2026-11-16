"""Original Garmin FIT versions and immutable completion evidence, separate from imports.

A result is ``downloaded`` (with its file), ``not_found``/``gone`` (permanent for the activity)
or ``invalid`` (for that raw list version only: a new list version is checked again).
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.guards import GuardSet

TABLES = ("garmin_activity_files", "garmin_activity_file_results")


class GarminActivityFile(Base):
    __tablename__ = TABLES[0]
    __table_args__ = (
        UniqueConstraint("activity_sha256", "sha256", name="uq_garmin_activity_files_version"),
        CheckConstraint("size >= 14 AND size <= 8388608", name="bounded_size"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    activity_id: Mapped[str] = mapped_column(Text, nullable=False)
    activity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("garmin_raw_records.id"), nullable=False)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GarminActivityFileResult(Base):
    __tablename__ = TABLES[1]
    __table_args__ = (
        UniqueConstraint("raw_record_id", name="uq_garmin_activity_file_results_raw"),
        Index("ix_garmin_activity_file_results_activity", "activity_sha256", "status"),
        CheckConstraint(
            "(status = 'downloaded' AND file_id IS NOT NULL AND reason IS NULL) OR "
            "(status IN ('not_found', 'gone') AND file_id IS NULL AND reason IS NULL) OR "
            "(status = 'invalid' AND file_id IS NULL AND reason IS NOT NULL)",
            name="outcome",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    activity_id: Mapped[str] = mapped_column(Text, nullable=False)
    activity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_record_id: Mapped[int] = mapped_column(ForeignKey("garmin_raw_records.id"), nullable=False)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("garmin_activity_files.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Only for ``invalid``: a fixed refusal code and the digest/size of a complete received
    # body (NULL when the body was refused while reading). The refused bytes are not kept.
    reason: Mapped[str | None] = mapped_column(String(32))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    content_size: Mapped[int | None] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


GUARDS = GuardSet()
for _table in TABLES:
    GUARDS.append_only(_table)

POSTGRESQL_DDL = GUARDS.postgresql_ddl
