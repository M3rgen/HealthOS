"""Raw archive of the Garmin Connect Personal API (migration 20261107_0024).

Garmin is the second source next to Google Health and gets its own tables, apart from
``raw_health_data`` and ``raw_api_responses``: those hold what Google returned, their readers
(deletion marking, coverage, identity, derived days, freshness) assume Google's record shape,
and a Garmin row under a Google data type would be marked source-deleted by the next complete
Google fetch of its window. Seven append-only tables (UPDATE/DELETE and PostgreSQL TRUNCATE are
refused, also for replica sessions) and separate bookkeeping tables:

* ``garmin_api_responses``: one row per HTTP exchange with ``personal.connectapi.garmin.com``
  (success, error status, retry), the body byte for byte with its SHA-256; the request as
  path and query string (never a header, so never the API key or the bearer token). A token
  exchange (``kind = 'token'``) keeps a fixed placeholder body instead of the token, with the
  SHA-256 and size of the bytes received (``app.garmin.client.GarminExchange``); rows written
  before that change hold the then-current, long expired, token.
* ``garmin_raw_records``: one row per record of one kind (``daily-summaries``, ``steps``,
  ``sleep``, ... ``activities``) and key (the calendar day, or the activity id) per distinct
  payload: fetching a day again with the same content adds nothing (unique
  ``(kind, key, body_sha256)``); a changed payload is a new row and the earlier one stays.
* ``garmin_record_observations``: each successful page's observation of a raw version,
  linked to that page's response. Readers take the last observed version per (kind, key),
  falling back to the largest raw id only for keys without observations. Existing responses
  are not reparsed to invent observations for the old archive.
* ``garmin_heart_sets`` and ``garmin_heart_members``: the daily heart samples produced by
  each changed normalization, including empty sets. The latest committed set selects visible
  samples independently of user quarantine; old sets and normalized samples are retained.
* ``garmin_daily_projections``: prepared daily row keys (NULL when invalid), with the
  selected raw input ids. Latest committed result per projection selects current values;
  missing inputs alone publish nothing, and pre-evidence v1/v2 archives remain readable.
* ``garmin_activity_results``: completed activity conversion keys (NULL when invalid),
  committed with normalized values and linked to the selected immutable raw record.
* ``garmin_pull_state``: per kind, when the last pull was tried and succeeded, its last error
  and the backfill checkpoint (``backfill_next``: the next day not fetched yet).
* ``garmin_normalization_state``: normalization success and unresolved failure, independent
  of HTTP pulls and their checkpoints; errors contain only a fixed code.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.guards import GuardSet
from app.models.state import RawJSON

RESPONSES = "garmin_api_responses"
RECORDS = "garmin_raw_records"
OBSERVATIONS = "garmin_record_observations"
HEART_SETS = "garmin_heart_sets"
HEART_MEMBERS = "garmin_heart_members"
DAILY_PROJECTIONS = "garmin_daily_projections"
ACTIVITY_RESULTS = "garmin_activity_results"
STATE = "garmin_pull_state"
NORMALIZATION_STATE = "garmin_normalization_state"
# The append-only tables (the evidence); the state table is bookkeeping.
TABLES = (
    RESPONSES,
    RECORDS,
    OBSERVATIONS,
    HEART_SETS,
    HEART_MEMBERS,
    DAILY_PROJECTIONS,
    ACTIVITY_RESULTS,
)
_ID = BigInteger()


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


class GarminRecordObservation(Base):
    __tablename__ = OBSERVATIONS
    __table_args__ = (
        Index("ix_garmin_observations_record_seen", "raw_record_id", "observed_at", "id"),
        UniqueConstraint("response_id", "raw_record_id", name="uq_garmin_observations_response"),
    )

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    raw_record_id: Mapped[int] = mapped_column(_ID, ForeignKey(f"{RECORDS}.id"), nullable=False)
    response_id: Mapped[int] = mapped_column(_ID, ForeignKey(f"{RESPONSES}.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GarminHeartSet(Base):
    """A committed daily normalization, including an empty set; latest id per source wins."""

    __tablename__ = HEART_SETS
    __table_args__ = (Index("ix_garmin_heart_sets_source_id", "source_record_id", "id"),)

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    source_record_id: Mapped[str] = mapped_column(String(160), nullable=False)
    raw_record_id: Mapped[int] = mapped_column(_ID, ForeignKey(f"{RECORDS}.id"), nullable=False)
    members_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class GarminHeartMember(Base):
    """Sample keys produced by a normalization; source removal never changes quarantine."""

    __tablename__ = HEART_MEMBERS

    set_id: Mapped[int] = mapped_column(_ID, ForeignKey(f"{HEART_SETS}.id"), primary_key=True)
    sample_key: Mapped[str] = mapped_column(String(255), primary_key=True)


class GarminDailyProjection(Base):
    """One completed daily projection; invalid results retain all previous evidence."""

    __tablename__ = DAILY_PROJECTIONS
    __table_args__ = (Index("ix_garmin_daily_projections_source_id", "source_record_id", "id"),)

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    source_record_id: Mapped[str] = mapped_column(String(160), nullable=False)
    # kind -> selected immutable GarminRawRecord.id, including alternate input kinds.
    raw_record_ids: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    normalized_key: Mapped[str | None] = mapped_column(String(255))


class GarminActivityResult(Base):
    """A completed activity conversion; absence of a key means invalid, never deleted."""

    __tablename__ = ACTIVITY_RESULTS
    __table_args__ = (Index("ix_garmin_activity_results_source_id", "source_record_id", "id"),)

    id: Mapped[int] = mapped_column(_ID, primary_key=True, autoincrement=True)
    source_record_id: Mapped[str] = mapped_column(String(160), nullable=False)
    raw_record_id: Mapped[int] = mapped_column(_ID, ForeignKey(f"{RECORDS}.id"), nullable=False)
    normalized_key: Mapped[str | None] = mapped_column(String(255))


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


class GarminNormalizationState(Base):
    __tablename__ = NORMALIZATION_STATE
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "last_error_code IS NULL OR last_error_code = 'garmin_normalization_failed'",
            name="safe_error_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Non-NULL until a normalization succeeds, even if a later HTTP pull succeeds.
    last_error_code: Mapped[str | None] = mapped_column(String(40))


GUARDS = GuardSet()
for _table in TABLES:
    GUARDS.append_only(_table)

FUNCTION_BODIES = GUARDS.functions
POSTGRESQL_DDL = GUARDS.postgresql_ddl
