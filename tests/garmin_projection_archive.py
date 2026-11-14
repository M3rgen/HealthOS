"""Synthetic Garmin daily archives built through the production writers, and plan work counts.

Days repeat a ten-day pattern: published once (A), republished A -> B, A -> B -> A, observed
legacy content with normalized rows but no projection evidence, and an empty day. Everything
except the legacy rows goes through ``store_page`` + ``normalize_window``; the legacy rows use
the normalizer's own ``build_rows``/``write_rows`` without writing a projection, which is what
an archive from before daily projections looks like.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from garmin_records import record_page, sleep
from sqlalchemy import event, text

from app.core.config import Settings
from app.services.providers.garmin_daily_values import SCALARS
from app.services.providers.garmin_ingestion import GarminSource
from app.services.providers.garmin_kinds import BY_NAME
from app.services.providers.garmin_normalize import build_rows, normalize_window, write_rows

START, SEEN = date(2000, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)
CHUNK = 250
# Metric -> the raw kind and member its first source input reads.
INPUTS = {"steps": ("steps", ("totalSteps",))} | {
    metric: definition[3][0] for metric, definition in SCALARS.items()
}
INPUTS["sleep"] = ("sleep", ("sleepTimeSeconds",))
AB, ABA, LEGACY, EMPTY = 3, 6, 8, 9


def day_case(index: int) -> int:
    return index % 10


def payload(kind: str, path: tuple[str, ...], day: date, value: object) -> dict:
    if kind == "sleep":
        body = sleep(day, seconds=int(value) * 600)
        end = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=8)
        body["sleepEndTimestampGMT"] = int(end.timestamp() * 1000)
        body["sleepStartTimestampGMT"] = int((end - timedelta(hours=7)).timestamp() * 1000)
        return body
    body: dict = {path[-1]: value}
    for name in reversed(path[:-1]):
        body = {name: body}
    return {"calendarDate": day.isoformat(), **body}


async def store(sessions, kind: str, records: list[dict], now: datetime) -> None:
    # A one-day endpoint keys its record by the page's day: one page per record.
    pages = [[record] for record in records] if BY_NAME[kind].mode == "day" else [records]
    source = GarminSource(Settings(_env_file=None), sessions, clock=lambda: now)
    # Keep one production response per day endpoint, but commit this bounded chunk
    # together. Thousands of session/commit cycles would measure fixture overhead.
    async with sessions.begin() as session:
        for page in pages:
            await source.store_raw(session, BY_NAME[kind], record_page(kind, page, now))


async def grow_archive(sessions, metric: str, first: int, last: int, *, all_legacy=False) -> None:
    """Add days ``START + first`` .. ``START + last - 1`` following the ten-day pattern."""
    kind, path = INPUTS[metric]
    for offset in range(first, last, CHUNK):
        days = [START + timedelta(days=i) for i in range(offset, min(offset + CHUNK, last))]
        pattern = {day: LEGACY if all_legacy else day_case((day - START).days) for day in days}
        passes = (
            [day for day in days if pattern[day] not in (LEGACY, EMPTY)],
            [day for day in days if pattern[day] in (AB, ABA)],
            [day for day in days if pattern[day] == ABA],
        )
        for version, selected in enumerate(passes):
            if not selected:
                continue
            value = 43 if version == 1 else 42
            records = [payload(kind, path, day, value + day.day) for day in selected]
            await store(sessions, kind, records, SEEN + timedelta(minutes=version))
            async with sessions.begin() as session:
                await normalize_window(session, days[0], days[-1], activities=False)
        legacy = [day for day in days if pattern[day] == LEGACY]
        if not legacy:
            continue
        bodies = {day.isoformat(): payload(kind, path, day, 40 + day.day) for day in legacy}
        await store(sessions, kind, list(bodies.values()), SEEN + timedelta(minutes=3))
        rows, _ = build_rows({kind: bodies}, legacy[0], legacy[-1], "Garmin")
        async with sessions.begin() as session:
            for model, values in rows.items():
                await write_rows(session, model, values)


async def analyze(engine) -> None:
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("ANALYZE"))


def _nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


def plan_summary(plan: dict) -> dict:
    """Rows every node produced or discarded, over all of its loops (EXPLAIN prints per loop)."""
    work = removed = 0
    for node in _nodes(plan["Plan"]):
        loops = node.get("Actual Loops", 0)
        dropped = node.get("Rows Removed by Join Filter", 0)
        removed += dropped * loops
        work += loops * (
            node.get("Actual Rows", 0)
            + dropped
            + node.get("Rows Removed by Filter", 0)
            + node.get("Rows Removed by Index Recheck", 0)
        )
    return {
        "work": round(work),
        "join_filter_removed": round(removed),
        "rows": plan["Plan"]["Actual Rows"],
    }


async def explain(connection, statement) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) the statement exactly as the driver receives it."""
    sync = connection.sync_connection

    def prefixed(conn, cursor, sql, parameters, context, executemany):
        return f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", parameters

    event.listen(sync, "before_cursor_execute", prefixed, retval=True)
    try:
        result = await connection.execute(statement)
        found = result.cursor.fetchone()[0]
    finally:
        event.remove(sync, "before_cursor_execute", prefixed)
    plan = json.loads(found) if isinstance(found, str) else found
    return plan_summary(plan[0])
