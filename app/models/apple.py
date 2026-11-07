"""Raw archive of Apple Health data (migration 20261107_a001).

Apple Health reaches HealthOS two ways, both the owner's own data and neither a pull from an
external service: the Health app's export (``export.zip``/``export.xml``, uploaded from the
local UI or imported with the CLI; channel ``export``) and the Health Auto Export iOS app's
REST API automation (a POST from the owner's phone; channel ``hae``). Like Garmin, Apple gets
its own tables apart from ``raw_health_data`` (Google's shape, Google's readers). Two
append-only tables (UPDATE/DELETE and PostgreSQL TRUNCATE are refused, also for replica
sessions) and two bookkeeping tables:

* ``apple_payloads``: every distinct Health Auto Export request body, byte for byte, with its
  SHA-256 (unique: a body sent again is not stored twice) and the app's ``automation-*``
  headers (never ``Authorization``).
* ``apple_raw_records``: one row per record (an export.xml ``Record``/``Workout``/
  ``ActivitySummary``, or a Health Auto Export metric point or workout) and distinct payload:
  unique ``(channel, kind, key, body_sha256)``. Importing the same export again adds nothing;
  a changed payload of the same key is a new row and the earlier one stays. Readers take the
  newest row per key.
* ``apple_imports``: one row per upload, CLI import or receiver request: status, progress and
  counts (bookkeeping, updated while the import runs).
* ``apple_sources``: per Health source (``sourceName``): device model, first and last import,
  newest data time and data types (bookkeeping for Cihazlarım, kept current by the archive).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.state import RawJSON

# Register the shared refusal function before this module's trigger listeners.
import app.models.raw_versions  # noqa: E402, F401  isort: skip

PAYLOADS = "apple_payloads"
RECORDS = "apple_raw_records"
IMPORTS = "apple_imports"
SOURCES = "apple_sources"
# The append-only tables (the evidence); the other two are bookkeeping.
TABLES = (PAYLOADS, RECORDS)
_ID = BigInteger().with_variant(Integer(), "sqlite")


class ApplePayload(Base):
    __tablename__ = PAYLOADS

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # The sender's descriptive headers (automation-name, automation-period, ...); no secret.
    headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    body_size: Mapped[int] = mapped_column(Integer, nullable=False)


class AppleImport(Base):
    """One upload, CLI import or receiver request and its progress (bookkeeping)."""

    __tablename__ = IMPORTS
    __table_args__ = (Index("ix_apple_imports_started", "started_at"),)

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # ``upload`` (UI), ``cli`` or ``receiver``.
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bytes_read: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    records_read: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    records_new: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rows_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # The export's own date (export.xml <ExportDate>), when it says one.
    export_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AppleRawRecord(Base):
    __tablename__ = RECORDS
    __table_args__ = (
        UniqueConstraint(
            "channel", "kind", "key", "body_sha256", name="uq_apple_raw_records_version"
        ),
        Index("ix_apple_raw_records_key", "channel", "kind", "key"),
        Index("ix_apple_raw_records_type_start", "type", "start_time"),
    )

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # HealthKit identifier (HKQuantityTypeIdentifierStepCount, HKWorkoutActivityTypeRunning)
    # or Health Auto Export's metric name (step_count).
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    # SHA-256 of the record's identity fields (app/apple/records.py).
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # The record as received (jsonb on PostgreSQL, like raw_json).
    body: Mapped[dict] = mapped_column(RawJSON, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255))
    device: Mapped[str | None] = mapped_column(String(255))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The receiver body (apple_payloads.id) or the import (apple_imports.id) it came in.
    payload_id: Mapped[int | None] = mapped_column(_ID, ForeignKey(f"{PAYLOADS}.id"))
    import_id: Mapped[int | None] = mapped_column(_ID, ForeignKey(f"{IMPORTS}.id"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AppleSource(Base):
    """Per Health source: what Cihazlarım shows (bookkeeping, kept by the archive)."""

    __tablename__ = SOURCES

    source_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    device: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sorted lists: the record types and the channels this source arrived through.
    data_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    channels: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    records: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


PG_TRIGGER_SQL = {}
PROTECTIVE_TRIGGERS = []
SQLITE_TRIGGER_SQL = {}
for _table in TABLES:
    for _suffix, _events, _level in (
        ("append_only", "UPDATE OR DELETE", "ROW"),
        ("no_truncate", "TRUNCATE", "STATEMENT"),
    ):
        _name = f"{_table}_{_suffix}"
        PG_TRIGGER_SQL[_name] = (
            _table,
            f"CREATE TRIGGER {_name}\nBEFORE {_events} ON {_table} FOR EACH {_level}\n"
            "EXECUTE FUNCTION healthos_refuse_change()",
        )
        PROTECTIVE_TRIGGERS.append((_table, _name, "healthos_refuse_change"))
    for _operation in ("UPDATE", "DELETE"):
        _name = f"{_table}_no_{_operation.lower()}"
        SQLITE_TRIGGER_SQL[_name] = (
            _table,
            f"CREATE TRIGGER {_name}\nBEFORE {_operation} ON {_table}\nBEGIN\n"
            f"    SELECT RAISE(ABORT, '{_table} keeps every record: {_operation} refused');\nEND",
        )
# healthos_refuse_change() belongs to 0011 and is verified with its other guards.
FUNCTION_BODIES: dict[str, str] = {}
POSTGRESQL_DDL = (
    *(sql for _, sql in PG_TRIGGER_SQL.values()),
    *(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {name}"
        for table, name, _ in PROTECTIVE_TRIGGERS
    ),
)
SQLITE_DDL = tuple(sql for _, sql in SQLITE_TRIGGER_SQL.values())
for _dialect, _statements in (("postgresql", POSTGRESQL_DDL), ("sqlite", SQLITE_DDL)):
    for _statement in _statements:
        event.listen(Base.metadata, "after_create", DDL(_statement).execute_if(dialect=_dialect))
