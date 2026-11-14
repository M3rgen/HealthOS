"""Independent pre-739 raw-input query; do not call the production resolver."""

from sqlalchemy import String, cast, func, select

from app.models.garmin import GarminHeartSet, GarminRawRecord
from app.models.health import HeartRate
from app.services.providers.garmin_current import current_record_ids


def raw_samples(query):
    sources = (
        query.with_only_columns(HeartRate.source_record_id, maintain_column_froms=True)
        .order_by(None)
        .distinct()
        .subquery()
    )
    completed = (
        select(GarminHeartSet.raw_record_id)
        .where(GarminHeartSet.source_record_id == sources.c.source_record_id)
        .order_by(GarminHeartSet.id.desc())
        .limit(1)
        .correlate(sources)
        .scalar_subquery()
    )
    raw = GarminRawRecord
    legacy = (
        select(raw.id)
        .where(
            raw.id.in_(current_record_ids(raw.kind == "heart-rate")),
            "heart-rate:" + raw.key == sources.c.source_record_id,
        )
        .correlate(sources)
        .scalar_subquery()
    )
    inputs = select(func.coalesce(completed, legacy)).select_from(sources)
    return select(raw.key, cast(raw.body.op("->")("heartRateValues"), String)).where(
        raw.id.in_(inputs)
    )
