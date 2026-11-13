"""Bounded health tools: source reads and a disposable local derived cache."""

import json
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.json_values import json_values
from app.db.contracts import SessionFactory
from app.db.dialect import database
from app.models.eligibility import raw_counted
from app.models.identity_queries import plan_each_query
from app.models.state import RawHealthData
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.dashboard_views.devices import device_report
from app.services.interpretation.archive_query import ArchiveQuery as ArchiveQuery
from app.services.interpretation.derived import build_range, derived_range
from app.services.interpretation.health_interpretation import derived_window, interpret, refusal
from app.services.interpretation.interpretation_types import ACTIONS
from app.services.readers import DashboardQuery
from app.services.synchronization.coverage import coverage_response

from .codex_chat_thread import HEALTH_TOOL as HEALTH_TOOL

DERIVED_BUDGET_BYTES = 64 * 1024
# The card's own fields (timezone, detail, reason codes, sizes, suggested range).
ENVELOPE_RESERVE_BYTES = 1024
# Per-record detail; the summary keeps each stream's value, state and uncertainties.
DETAIL_KEYS = ("observations", "source_metadata", "measurement_metadata")
SLEEP_DETAIL_KEYS = ("sessions", "episodes", "timeline")


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def _summary_group(group: dict) -> dict:
    row = {key: value for key, value in group.items() if key not in DETAIL_KEYS}
    row["observation_count"] = len(group.get("observations", []))
    if group.get("sleep") is not None:
        row["sleep"] = {
            key: value for key, value in group["sleep"].items() if key not in SLEEP_DETAIL_KEYS
        }
    dates = {item.get("measured_at") for item in group.get("observations", [])}
    if group.get("source") == "manuel" and len(dates) == 1:
        row["measured_at"] = dates.pop()
    return row


def _summary_day(item: dict) -> dict:
    result = item.get("result")
    if result is None:
        return item
    return item | {
        "result": result
        | {
            "streams": [_summary_group(group) for group in result["streams"]],
            "manual_profile": [_summary_group(group) for group in result["manual_profile"]],
        }
    }


def _suggested(days: list[dict]) -> dict | None:
    """The latest days whose items, in this representation, fit the budget on their own."""
    room, count = DERIVED_BUDGET_BYTES - ENVELOPE_RESERVE_BYTES, 0
    for item in reversed(days):
        room -= _size(item) + 2  # the ", " separator
        if room < 0:
            break
        count += 1
    if not count or count == len(days):
        return None
    return {"start_date": days[-count]["date"], "end_date": days[-1]["date"]}


def bounded_derived(card: dict) -> dict:
    """The daily derived card within its byte budget, never a clipped JSON document.

    Detail is moved, not discarded: a narrower daily range, sleep and records still
    return observations, sleep timelines and the original payloads.
    """
    full = card | {"detail": "full"}
    full_size = _size(full)
    if full_size <= DERIVED_BUDGET_BYTES:
        return full
    days = [_summary_day(item) for item in card["days"]]
    summary = card | {
        "days": days,
        "detail": "summary",
        "reason_codes": ["derived_detail_budget_exceeded"],
        "budget_bytes": DERIVED_BUDGET_BYTES,
        "size_bytes": full_size,
        "suggested_range": _suggested(card["days"]),
    }
    summary_size = _size(summary)
    if summary_size <= DERIVED_BUDGET_BYTES:
        return summary
    return card | {
        "days": [],
        "detail": "omitted",
        "reason_codes": ["derived_budget_exceeded"],
        "budget_bytes": DERIVED_BUDGET_BYTES,
        "size_bytes": summary_size,
        "suggested_range": _suggested(days),
    }


async def read_health(sessions: SessionFactory | None, arguments: dict) -> dict:
    try:
        query = ArchiveQuery.model_validate(arguments)
    except (ValueError, KeyError):
        if isinstance(arguments, dict) and arguments.get("action") in ACTIONS:
            return refusal("invalid_query")
        raise
    if sessions is None:
        raise RuntimeError("Health archive session factory is not configured")
    window = (
        query.date_range()
        if query.action == "daily"
        else derived_window(query)
        if query.action in ACTIONS
        else None
    )
    if window is not None:
        # Cold/pending days are built first, one short transaction per day: the build lock
        # never spans a whole range, and no day is built from a REPEATABLE READ snapshot.
        await build_range(sessions, *window, query.timezone)
    async with sessions() as session:
        if query.action in ACTIONS:
            # One source/cache/eligibility snapshot across every scientific read.
            await database(session).control(
                session, text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            )
        await plan_each_query(session)
        if query.action == "devices":
            return dict(await device_report(session))
        if query.action == "inventory":
            return await _inventory(session, query)
        start_date, end_date = query.date_range()
        if query.action in ACTIONS:
            result = await interpret(session, query)
            await session.commit()  # disposable derived cache only
            return result
        if query.action == "coverage":
            return await coverage_response(
                session, start_date, end_date, query.timezone, query.data_type
            )
        if query.action == "daily":
            result = await dashboard_data(
                session,
                query=DashboardQuery(end_date, (end_date - start_date).days + 1, query.timezone),
            )
            daily_result = dict(result)
            daily_result["derived"] = bounded_derived(
                await derived_range(session, start_date, end_date, query.timezone)
            )
            await session.commit()  # only the local, disposable read model is written
            return json_values(daily_result)
        return await _records(session, query)


async def _inventory(session: AsyncSession, query: ArchiveQuery) -> dict:
    rows = await session.execute(
        select(
            RawHealthData.data_type,
            func.count(),
            func.min(RawHealthData.source_start_time),
            func.max(RawHealthData.source_start_time),
            func.count(RawHealthData.source_start_time),
        )
        .where(raw_counted())
        .group_by(RawHealthData.data_type)
        .order_by(RawHealthData.data_type)
    )
    return json_values(
        {
            "today": datetime.now(ZoneInfo(query.timezone)).date(),
            "timezone": query.timezone,
            "streams": [
                {
                    "data_type": kind,
                    "count": count,
                    "first": first,
                    "last": last,
                    "undated_count": count - dated,
                }
                for kind, count, first, last, dated in rows
            ],
            "note": "Karantinadaki ve kaynakta silinen kayıtlar hariç. Tarihler kaynak zamanı.",
        }
    )


async def _records(session: AsyncSession, query: ArchiveQuery) -> dict:
    start_date, end_date = query.date_range()
    zone = ZoneInfo(query.timezone)
    start = datetime.combine(start_date, time.min, zone).astimezone(UTC)
    stop = datetime.combine(end_date + timedelta(days=1), time.min, zone).astimezone(UTC)
    result = await session.scalars(
        select(RawHealthData)
        .where(
            RawHealthData.data_type == query.data_type,
            raw_counted(data_types=(query.data_type,)),
            RawHealthData.source_start_time < stop,
            func.coalesce(RawHealthData.source_end_time, RawHealthData.source_start_time) >= start,
        )
        .order_by(RawHealthData.source_start_time, RawHealthData.id)
        .offset(query.offset)
        .limit(query.limit + 1)
    )
    rows = list(result)
    return json_values(
        {
            "data_type": query.data_type,
            "timezone": query.timezone,
            "start_date": query.start_date,
            "end_date": query.end_date,
            "records": [
                {
                    "start": row.source_start_time,
                    "end": row.source_end_time,
                    "source_timezone": row.source_timezone,
                    "payload": row.raw_json,
                }
                for row in rows[: query.limit]
            ],
            "next_offset": query.offset + query.limit if len(rows) > query.limit else None,
            "note": "Tarihsiz, karantinadaki ve kaynakta silinen kayıtlar hariç.",
        }
    )
