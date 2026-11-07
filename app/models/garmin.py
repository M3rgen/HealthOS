"""Raw archive of the Garmin Connect Personal API (migration 20261107_0024).

Garmin is the second source next to Google Health and gets its own tables, apart from
``raw_health_data`` and ``raw_api_responses``: those hold what Google returned, their readers
(deletion marking, coverage, identity, derived days, freshness) assume Google's record shape,
and a Garmin row under a Google data type would be marked source-deleted by the next complete
Google fetch of its window. Two append-only tables (UPDATE/DELETE and PostgreSQL TRUNCATE are
refused, also for replica sessions) and one bookkeeping table:

* ``garmin_api_responses``: one row per HTTP exchange with ``personal.connectapi.garmin.com``
  (success, error status, retry), the body byte for byte with its SHA-256; the request as
  path and query string (never a header, so never the API key or the bearer token).
* ``garmin_raw_records``: one row per record of one kind (``daily-summaries``, ``steps``,
  ``sleep``, ... ``activities``) and key (the calendar day, or the activity id) per distinct
  payload: fetching a day again with the same content adds nothing (unique
  ``(kind, key, body_sha256)``); a changed payload is a new row and the earlier one stays.
  Readers take the newest row per (kind, key).
* ``garmin_pull_state``: per kind, when the last pull was tried and succeeded, its last error
  and the backfill checkpoint (``backfill_next``: the next day not fetched yet).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    Date,
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

RESPONSES = "garmin_api_responses"
RECORDS = "garmin_raw_records"
STATE = "garmin_pull_state"
# The append-only tables (the evidence); the state table is bookkeeping.
TABLES = (RESPONSES, RECORDS)
_ID = BigInteger().with_variant(Integer(), "sqlite")


class GarminApiResponse(Base):
    __tablename__ = RESPONSES
    __table_args__ = (Index("ix_garmin_api_responses_kind_fetched", "kind", "fetched_at"),)

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The pull kind the request served (``token`` for the key exchange), and the run.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    context_id: Mapped[str | None] = mapped_column(String(160))
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    # SHA-256 of ``method path?query``: the same request again has the same digest.
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_size: Mapped[int] = mapped_column(Integer, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text)


class GarminRawRecord(Base):
    __tablename__ = RECORDS
    __table_args__ = (
        UniqueConstraint("kind", "key", "body_sha256", name="uq_garmin_raw_records_version"),
        Index("ix_garmin_raw_records_kind_key", "kind", "key"),
    )

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # The calendar day (YYYY-MM-DD) the record describes, or the activity id.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # The parsed record as Garmin returned it (jsonb on PostgreSQL, like raw_json).
    body: Mapped[dict | list] = mapped_column(RawJSON, nullable=False)
    # SHA-256 of the record's canonical JSON: the same content again is the same digest.
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # The exchange (garmin_api_responses.id) this payload was first read from.
    response_id: Mapped[int | None] = mapped_column(_ID, ForeignKey(f"{RESPONSES}.id"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GarminPullState(Base):
    """Per kind: the last attempt and success, the last error, the backfill checkpoint."""

    __tablename__ = STATE

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The running or finished backfill: its bounds, and the next day not fetched yet
    # (NULL when the backfill is complete). A new --since/--until starts over.
    backfill_since: Mapped[date | None] = mapped_column(Date)
    backfill_until: Mapped[date | None] = mapped_column(Date)
    backfill_next: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


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
