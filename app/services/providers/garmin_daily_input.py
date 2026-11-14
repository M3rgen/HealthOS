"""Resolve immutable daily inputs through indexed projection and legacy lookups."""

from collections.abc import Iterable
from datetime import date

from sqlalchemy import BigInteger, Select, case, func, select

from app.models.garmin import GarminDailyProjection, GarminRawRecord
from app.models.health import (
    HeartRateVariability,
    OxygenSaturation,
    RestingHeartRate,
    SleepSession,
    Steps,
)

from .garmin_current import current_record_ids

type _DailyModel = type[
    Steps | RestingHeartRate | HeartRateVariability | OxygenSaturation | SleepSession
]


def daily_input_id(
    model: _DailyModel,
    raw_kind: str,
    prefix: str,
    first: date | None = None,
    last: date | None = None,
):
    """Resolve one raw id without joining the entire daily projection archive.

    Probe the newest completed projection through its source/id index. A completed
    projection with no input for this kind must remain missing; it must never fall
    back to a newer, unpublished observation. Only pre-projection rows use the
    observation/max-id selection, with the same optional calendar bounds as before.

    Keep kind and key bare on the indexed side of the legacy equality. Concatenating
    the prefix onto every raw key forces a full archive scan per legacy daily row.
    Each correlated scalar probe ends at one indexed row. The caller joins only
    the selected raw primary key, retaining its JSON-member and presence semantics.
    No projection relation remains for an underestimated outer row count to rescan.
    """
    state, raw = GarminDailyProjection, GarminRawRecord
    completed = (
        select(state.id)
        .where(state.source_record_id == model.source_record_id)
        .order_by(state.id.desc())
        .limit(1)
        .correlate(model)
    )
    published = completed.with_only_columns(
        state.raw_record_ids[raw_kind].as_string().cast(BigInteger)
    )
    criteria = [raw.kind == raw_kind]
    if first is not None:
        criteria.append(raw.key >= str(first))
    if last is not None:
        criteria.append(raw.key <= str(last))
    legacy = (
        select(raw.id)
        .where(
            func.substr(model.source_record_id, 1, len(prefix) + 1) == prefix + ":",
            raw.kind == raw_kind,
            raw.key == func.substr(model.source_record_id, len(prefix) + 2),
            raw.id.in_(current_record_ids(*criteria)),
        )
        .correlate(model)
        .scalar_subquery()
    )
    return case(
        (completed.scalar_subquery().is_not(None), published.scalar_subquery()),
        else_=legacy,
    )


def daily_input_rows(
    query: Select,
    model: _DailyModel,
    prefix: str,
    kinds: Iterable[str],
    first: date | None = None,
    last: date | None = None,
):
    """Resolve inputs once per eligible measurement before joining raw primary keys.

    PostgreSQL must materialize these small keys, not substitute correlated scalar
    expressions back into join conditions under a low Garmin cardinality estimate.
    This makes the raw primary-key join parameterizable even in Google-heavy tables.
    Other engines retain normal indexed probes. Nest the CTE in the derived relation
    so callers still select only normalized columns and individual JSON members.
    """
    resolved = (
        query.add_columns(
            *(
                daily_input_id(model, kind, prefix, first, last).label(f"input_{i}")
                for i, kind in enumerate(kinds)
            )
        )
        .cte("daily_inputs", nesting=True)
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )
    return select(resolved).subquery(model.__tablename__)
