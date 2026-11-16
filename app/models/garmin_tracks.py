"""Disposable FIT track packages: one per distinct original FIT content and method.

Rebuilt from the original archives at any time (``python -m app.cli.garmin_tracks
--rebuild``), so, like ``google_heart_hours``, these read models carry no append-only
guard and a downgrade may drop them (app/db/downgrade_guard.py ``DISPOSABLE_TABLES``).
"""

from sqlalchemy import JSON, CheckConstraint, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TABLES = ("garmin_tracks", "garmin_track_sessions")


class GarminTrack(Base):
    __tablename__ = TABLES[0]
    __table_args__ = (
        CheckConstraint("method_version > 0 AND sample_count >= 0", name="valid_counts"),
        CheckConstraint("status IN ('ready', 'invalid')", name="valid_status"),
    )

    # SHA-256 of the FIT bytes, as in garmin_activity_files.sha256 and a manual upload's
    # raw_json["garmin-fit-file"]["sha256"]; identical bytes from both sources share it.
    source_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    method_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    samples: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    laps: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    events: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)


class GarminTrackSession(Base):
    """Which uploaded FIT content produced a FIT workout (``exercise_sessions``).

    A manual upload's workout row does not carry its file digest. Parsing the archived
    upload again yields the same ``fit:`` source keys, so this link is derived from the
    raw FIT row alone and rebuilt with the packages; Garmin API workouts link through
    ``garmin_activity_file_results`` instead.
    """

    __tablename__ = TABLES[1]
    __table_args__ = (Index("ix_garmin_track_sessions_source", "source_sha256"),)

    # exercise_sessions.source_record_id of the FIT workout (``fit:<record key>``).
    source_record_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
