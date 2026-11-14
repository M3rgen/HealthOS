"""Independent pre-fix queries: keep the former joins as an equivalence oracle."""

from sqlalchemy import JSON, BigInteger, String, case, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import aliased

from app.core.providers import GARMIN
from app.models.garmin import GarminDailyProjection, GarminRawRecord
from app.models.health import SleepSession, Steps
from app.services.providers.garmin_current import current_record_ids
from app.services.providers.garmin_daily_values import SCALARS
from app.services.providers.garmin_versions import current_row


def projection(model):
    return (
        select(GarminDailyProjection.id)
        .where(GarminDailyProjection.source_record_id == model.source_record_id)
        .order_by(GarminDailyProjection.id.desc())
        .limit(1)
        .correlate(model)
        .scalar_subquery()
    )


def input_id(model, prefix, raw_kind, first=None, last=None):
    raw, state = GarminRawRecord, GarminDailyProjection
    criteria = [raw.kind == raw_kind]
    if first is not None:
        criteria.extend((raw.key >= str(first), raw.key <= str(last)))
    legacy = (
        select(raw.id)
        .where(
            raw.id.in_(current_record_ids(*criteria)),
            prefix + ":" + raw.key == model.source_record_id,
        )
        .correlate(model)
        .scalar_subquery()
    )
    return case(
        (state.id.is_not(None), state.raw_record_ids[raw_kind].as_string().cast(BigInteger)),
        else_=legacy,
    )


def step_query(first, last, provider=GARMIN):
    state, raw = GarminDailyProjection, GarminRawRecord
    # Return JSON text so the driver cannot underflow a numeric token to -0.0.
    # Decode just this member, never the entire daily body.
    source_steps = sql_cast(raw.body.op("->")("totalSteps"), String)
    return (
        select(Steps.source_record_id, Steps.count, source_steps)
        .outerjoin(state, state.id == projection(Steps))
        .outerjoin(
            raw,
            (Steps.provider == GARMIN) & (raw.id == input_id(Steps, "steps", "steps", first, last)),
        )
        .where(
            Steps.provider == provider,
            current_row(Steps),
            Steps.is_quarantined.is_(False),
            Steps.source_record_id >= f"steps:{first.isoformat()}",
            Steps.source_record_id <= f"steps:{last.isoformat()}",
        )
    )


def scalar_query(metric, first, last, provider=GARMIN):
    model, column, kind, inputs = SCALARS[metric]
    state = GarminDailyProjection
    query = select(model.source_record_id, column).outerjoin(state, state.id == projection(model))
    for index, (raw_kind, path) in enumerate(inputs):
        raw = aliased(GarminRawRecord, name=f"daily_input_{index}")
        query = query.outerjoin(
            raw,
            (model.provider == GARMIN) & (raw.id == input_id(model, kind, raw_kind, first, last)),
        )
        # Cast the final JSON member to text, bypassing the driver's JSON decoder.
        member = raw.body
        for name in path:
            member = member.op("->")(name)
        query = query.add_columns(sql_cast(member, String))
        if metric == "hrv_ms" and index == 0:
            query = query.add_columns(
                raw.id.is_not(None) & (raw.body != {}) & (raw.body != []) & (raw.body != JSON.NULL)
            )
    return query.where(
        model.provider == provider,
        current_row(model),
        model.is_quarantined.is_(False),
        model.source_record_id >= f"{kind}:{first.isoformat()}",
        model.source_record_id <= f"{kind}:{last.isoformat()}",
    )


def sleep_query(start, stop, provider=GARMIN):
    row, state = SleepSession, GarminDailyProjection
    query = select(row).outerjoin(state, state.id == projection(row))
    sources = []
    for kind in ("sleep", "sleep-summaries"):
        raw = aliased(GarminRawRecord, name=kind.replace("-", "_"))
        query = query.outerjoin(
            raw, (row.provider == GARMIN) & (raw.id == input_id(row, "sleep", kind))
        )
        sources.append(raw)
    primary, alternate = sources
    # The converter falls back by whole record, never by individual member.
    present = (
        primary.id.is_not(None)
        & (primary.body != {})
        & (primary.body != [])
        & (primary.body != JSON.NULL)
    )
    for member in ("sleepTimeSeconds", "awakeSleepSeconds"):
        query = query.add_columns(
            case(
                (present, sql_cast(primary.body.op("->")(member), String)),
                else_=sql_cast(alternate.body.op("->")(member), String),
            )
        )
    return query.where(
        row.provider == provider,
        current_row(row),
        row.is_quarantined.is_(False),
        row.end_time >= start,
        row.end_time < stop,
    ).order_by(row.end_time, row.id)
