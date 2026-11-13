"""Eligibility policy for dashboard queries, including the historical deletion option."""

from collections.abc import Collection

from sqlalchemy import and_, or_, select

from app.models.eligibility import normalized_counted, normalized_quarantined, raw_counted
from app.models.identity_queries import held_elsewhere, holds_live, key_held_elsewhere, superseded
from app.models.state import RawHealthData

from .dashboard_rows import DAILY_METRICS


class DashboardSources:
    # Every reader counts the same records (app/models/eligibility.py, ``normalized_counted`` and
    # ``raw_counted``): a live raw row, neither it nor its normalized row quarantined, not
    # superseded by a counted row (migration 20261021_0012). A normalized row without a raw
    # row is not counted. ``valid_sources_only`` (kept for older callers) asks for exactly
    # this, which is the default: it only overrides ``include_source_deleted``. With source
    # deletions included, a deleted row counts too, unless it is superseded or a live
    # duplicate it holds counts instead (never a deleted holder's value in place of a live
    # row's). ``metrics`` narrows a read to those daily values (a history page reads one); the
    # others stay ``None`` and their records are never fetched.
    def __init__(
        self, include_source_deleted: bool = False, metrics: Collection[str] = DAILY_METRICS
    ):
        self.include_source_deleted = include_source_deleted
        self.metrics = metrics
        self.any_superseded = or_(
            held_elsewhere(RawHealthData.id),
            and_(
                RawHealthData.source_deleted_at.is_not(None),
                or_(superseded(RawHealthData.id), holds_live(RawHealthData.id)),
            ),
        )

    def normalized(self, model, data_type: str):
        if not self.include_source_deleted:
            return normalized_counted(model, (data_type,))
        raw = select(RawHealthData.id).where(
            RawHealthData.data_type == data_type,
            RawHealthData.google_data_point_id == model.google_data_point_id,
            RawHealthData.is_quarantined.is_(False),
        )
        deleted_and_held = raw.where(
            RawHealthData.source_deleted_at.is_not(None),
            or_(superseded(RawHealthData.id), holds_live(RawHealthData.id)),
        )
        return and_(
            raw.exists(),
            ~key_held_elsewhere(data_type, model.google_data_point_id),
            ~deleted_and_held.exists(),
        )

    def raw(self, data_types):
        if self.include_source_deleted:
            # A quarantine on either side still hides the record (only deletion is let in).
            return and_(
                RawHealthData.is_quarantined.is_(False),
                ~self.any_superseded,
                *(~clause for clause in normalized_quarantined(RawHealthData, data_types)),
            )
        return raw_counted(data_types=data_types)
