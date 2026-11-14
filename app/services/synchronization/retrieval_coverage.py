"""Read bounded daily retrieval evidence, separately from present eligible values.

Unknown history stays unknown. A completed attempt must have a first-page root, a
contiguous token chain, a durable last page and an appended completion outcome. Mutable
job tables are not read. A day that no attempt touched is ``not_attempted`` only when it
begins after the first receipt was written (``receipt_horizon``): from then on every
Google read leaves a receipt, so the absence of one is evidence. Earlier days stay
``unknown``.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.timeutil import as_utc
from app.google_health.data_types import (
    CIVIL_AFTER,
    CIVIL_BEFORE,
    WAKE_DATED_TYPES,
    DataTypeSpec,
    RecordKind,
)
from app.models.eligibility import raw_counted
from app.models.identity_queries import plan_each_query
from app.models.retrieval import RetrievalEvent as Event
from app.models.retrieval import RetrievalReceipt as Receipt
from app.models.state import RawHealthData as Raw
from app.models.state import RawJSON
from app.parsers.common import parse_datetime
from app.schemas.status import Coverage
from app.services.readers.current_sleep import sleep_wake_window
from app.services.shared.query_ranges import CivilDateRange

from .coverage_reading import (
    COVERAGE_BUDGET_BYTES,
    METRICS,
    NORMALIZED,
    Metric,
    Window,
    google_streams,
    metric_exclusions,
    source_regime,
)
from .coverage_sleep import sleep_value_days
from .coverage_sources import source_streams

MAX_RECEIPTS = 2000
MAX_EVENTS = 5000
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_DAYS = 31
# Per type, including failed reads; a 31-day binary split needs at most 61 reads.
MAX_EVIDENCE_PIECES = 64


class CoverageLimit(ValueError):
    pass


def merge_ranges(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Half-open union: touching intervals join, even a one-second gap remains a gap."""
    merged = []
    for start, stop in sorted(ranges):
        if stop <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
        else:
            merged.append((start, stop))
    return merged


def covers(ranges: list[tuple[datetime, datetime]], start: datetime, stop: datetime) -> bool:
    return any(left <= start and right >= stop for left, right in merge_ranges(ranges))


async def evidence(session: AsyncSession, name: str, window: Window):
    """Read one type's full receipt lineage within its own evidence budgets."""
    # Include civil filter bounds in their own coordinate system, plus daily UTC dates.
    lower = window.start - CIVIL_BEFORE - timedelta(days=1)
    upper = window.stop + CIVIL_AFTER + timedelta(days=1)
    rows = list(
        (
            await session.scalars(
                select(Receipt)
                .where(
                    Receipt.data_type == name,
                    or_(Receipt.queried_from.is_(None), Receipt.queried_from < upper),
                    or_(Receipt.queried_to.is_(None), Receipt.queried_to > lower),
                )
                .order_by(Receipt.started_at, Receipt.id)
                .limit(MAX_RECEIPTS + 1)
            )
        ).all()
    )
    if len(rows) > MAX_RECEIPTS:
        raise CoverageLimit("coverage_receipt_limit")
    if not rows:
        return [], {}
    ids = [row.id for row in rows]
    events = list(
        (
            await session.scalars(
                select(Event)
                .options(defer(Event.returned_times))
                .where(Event.receipt_id.in_(ids))
                .order_by(Event.id)
                .limit(MAX_EVENTS + 1)
            )
        ).all()
    )
    if len(events) > MAX_EVENTS:
        raise CoverageLimit("coverage_evidence_limit")
    times = await _evidence_times(session, events, window)
    by_receipt = defaultdict(list)
    by_event = {}
    for event in events:
        by_receipt[event.receipt_id].append(event)
        by_event[event.id] = event
    receipts = {row.id: row for row in rows}

    result = []
    for receipt in rows:
        outcomes = by_receipt[receipt.id]
        completed = next((e for e in reversed(outcomes) if e.kind == "completed"), None)
        pages = _receipt_chain(receipt, set(), by_receipt, by_event, receipts)
        valid = bool(
            completed
            and pages
            and pages[-1].last_page
            and pages[-1].next_token_hash is None
            and pages[-1].id < completed.id
            and outcomes[-1].kind == "completed"
        )
        # Pagination can cross midnight (or resume days later). Its initial snapshot
        # cannot prove time that was still in the future when the first page began.
        root = receipts[pages[0].receipt_id] if pages else receipt
        result.append((receipt, pages or [], completed if valid else None, as_utc(root.started_at)))
    return result, times


async def _evidence_times(session: AsyncSession, events: list[Event], window: Window):
    # All metadata proves the token chain, but timestamps from other days are not read.
    # Include both civil-date UTC coordinates and the requested physical day bounds.
    read_start = min(window.start, datetime.combine(window.first, time.min, UTC))
    read_stop = max(window.stop, datetime.combine(window.end + timedelta(days=1), time.min, UTC))
    relevant = [
        e.id
        for e in events
        if e.kind == "page"
        and e.first_record_at is not None
        and e.last_record_at is not None
        and as_utc(e.first_record_at) < read_stop
        and as_utc(e.last_record_at) >= read_start
    ]
    times: dict[int, list] = {}
    if relevant:
        size = await session.scalar(
            select(func.coalesce(func.sum(func.length(cast(Event.returned_times, Text))), 0)).where(
                Event.id.in_(relevant)
            )
        )
        if (size or 0) > MAX_EVIDENCE_BYTES:
            raise CoverageLimit("coverage_evidence_limit")
        # Exact immutable ids: a writer appending new events cannot grow this read.
        times = {
            identifier: stamps
            for identifier, stamps in (
                await session.execute(
                    select(Event.id, Event.returned_times).where(Event.id.in_(relevant))
                )
            ).tuples()
        }
    return times


def _receipt_chain(
    receipt: Receipt,
    seen: set[uuid.UUID],
    by_receipt: dict[uuid.UUID, list[Event]],
    by_event: dict[int, Event],
    receipts: dict[uuid.UUID, Receipt],
) -> list[Event] | None:
    if len(seen) >= 100:
        raise CoverageLimit("coverage_lineage_limit")
    if receipt.id in seen or any(e.kind == "reset" for e in by_receipt[receipt.id]):
        return None
    seen = seen | {receipt.id}
    pages = []
    expected = receipt.resume_token_hash
    if not receipt.starts_first:
        parent = (
            by_event.get(receipt.parent_event_id) if receipt.parent_event_id is not None else None
        )
        if parent is None:
            return None
        previous = receipts.get(parent.receipt_id)
        if (
            not previous
            or parent.kind != "page"
            or not expected
            or parent.next_token_hash != expected
            or (
                previous.lane,
                previous.owner,
                previous.data_type,
                previous.requested_from,
                previous.requested_to,
                previous.basis,
                previous.access_method,
                previous.query_filter,
                previous.queried_from,
                previous.queried_to,
            )
            != (
                receipt.lane,
                receipt.owner,
                receipt.data_type,
                receipt.requested_from,
                receipt.requested_to,
                receipt.basis,
                receipt.access_method,
                receipt.query_filter,
                receipt.queried_from,
                receipt.queried_to,
            )
        ):
            return None
        prefix = _receipt_chain(previous, seen, by_receipt, by_event, receipts)
        if prefix is None or not prefix or prefix[-1].id != parent.id:
            return None
        pages.extend(prefix)
    elif expected is not None or receipt.parent_event_id is not None:
        return None
    seen_tokens = {page.next_token_hash for page in pages if page.next_token_hash}
    for event in by_receipt[receipt.id]:
        if event.kind == "page":
            if event.request_token_hash != expected or (pages and pages[-1].last_page):
                return None
            if event.next_token_hash and event.next_token_hash in seen_tokens:
                return None
            if event.next_token_hash:
                seen_tokens.add(event.next_token_hash)
            pages.append(event)
            expected = event.next_token_hash
    return pages


async def receipt_horizon(session: AsyncSession) -> datetime | None:
    """When retrieval evidence began: the earliest receipt of any type, or None."""
    first = await session.scalar(select(func.min(Receipt.started_at)))
    return as_utc(first) if first is not None else None


def unattempted(day: date, window: Window, horizon: datetime | None) -> str:
    """The state of a closed day no attempt touched: ``not_attempted`` only when every
    filter that could have reached it (the civil envelope included) lies after the
    horizon; otherwise nothing proves it was never read."""
    if horizon is None:
        return "unknown"
    earliest = min(
        datetime.combine(day, time.min, window.zone).astimezone(UTC),
        datetime.combine(day, time.min, UTC),
    )
    return "not_attempted" if earliest - CIVIL_BEFORE >= horizon else "unknown"


def daily_evidence(
    spec: DataTypeSpec,
    day: date,
    window: Window,
    attempts: list[tuple[Receipt, list[Event], Event | None, datetime]],
    times: dict[int, list[list[Any]]],
    receipts_since: datetime | None = None,
) -> tuple[str, int | None, str | None]:
    physical_start = datetime.combine(day, time.min, window.zone).astimezone(UTC)
    physical_stop = datetime.combine(day + timedelta(days=1), time.min, window.zone).astimezone(UTC)
    daily = spec.kind == RecordKind.daily
    lower = datetime.combine(day, time.min, UTC) if daily else physical_start
    upper = lower + timedelta(days=1) if daily else physical_stop
    ranges, empty_ranges = [], []
    attempted, returned, ambiguous = False, 0, False
    counted_pages = set()
    reason = None
    for receipt, pages, completed, horizon in attempts:
        touched, interval = _attempt_range(
            receipt,
            completed,
            horizon,
            day=EvidenceDay(lower=lower, upper=upper, physical_stop=physical_stop, daily=daily),
        )
        attempted |= touched
        if interval is None:
            continue
        left, right = interval
        ranges.append((left, right))
        unknown = any(page.unplaced_count for page in pages)
        if not unknown:
            empty_ranges.append((left, right))
        else:
            ambiguous = True
        returned += _returned_in_day(pages, times, counted_pages, lower, upper)
    if not spec.time_filtered:
        return "unknown", None, "no_time_filter"
    if spec.name == "electrocardiogram":
        # The request has no upper bound. Daily *empty* proof is unsafe for malformed
        # timestamps; bounded days are still supported by a rooted last-page snapshot.
        reason = "lower_bound_filter"
    wanted_left, wanted_right = lower, upper
    if spec.kind == RecordKind.session and spec.name not in {"sleep", "electrocardiogram"}:
        wanted_left -= CIVIL_BEFORE
        wanted_right += CIVIL_AFTER
        reason = "civil_start_offset_envelope"
    if day >= window.today:
        return "incomplete" if attempted else "unknown", None, "open_day"
    if not attempted:
        return unattempted(day, window, receipts_since), None, reason
    if covers(ranges, wanted_left, wanted_right):
        if returned:
            return (
                "completed_with_records",
                None if ambiguous else returned,
                "returned_dates_unknown" if ambiguous else reason,
            )
        if not ambiguous and covers(empty_ranges, wanted_left, wanted_right):
            return "completed_empty", 0, reason
        if ambiguous:
            return "unknown", None, "returned_dates_unknown"
    return "incomplete", None, reason


@dataclass(frozen=True, slots=True)
class EvidenceDay:
    lower: datetime
    upper: datetime
    physical_stop: datetime
    daily: bool


def _attempt_range(
    receipt: Receipt, completed: Event | None, horizon: datetime, *, day: EvidenceDay
) -> tuple[bool, tuple[datetime, datetime] | None]:
    """Return whether the attempt touches this day and its completed query interval."""
    if receipt.queried_from is None:
        return False, None
    left = as_utc(receipt.queried_from)
    right = as_utc(receipt.queried_to) if receipt.queried_to else day.physical_stop
    if receipt.basis == "civil_start":
        # Merge query ranges before shrinking: adjacent requests can cover a whole day.
        wanted_left, wanted_right = day.lower - CIVIL_BEFORE, day.upper + CIVIL_AFTER
    else:
        wanted_left, wanted_right = day.lower, day.upper
    if right <= wanted_left or left >= wanted_right:
        return False, None
    if not completed:
        return True, None
    at = min(as_utc(completed.occurred_at), horizon)
    if day.daily or receipt.basis == "civil_start":
        # Today's date/civil filter cannot prove a closed day tomorrow.
        if at < day.physical_stop:
            return True, None
    else:
        right = min(right, at)
    return True, (left, right)


def _returned_in_day(
    pages: list[Event],
    times: dict[int, list[list[Any]]],
    counted_pages: set[int],
    lower: datetime,
    upper: datetime,
) -> int:
    returned = 0
    for page in pages:
        if page.id in counted_pages:
            continue
        counted_pages.add(page.id)
        for stamp, count in times.get(page.id, ()):
            instant = parse_datetime(stamp)
            if instant is not None and lower <= instant < upper:
                returned += count
    return returned


async def presence(session: AsyncSession, spec: DataTypeSpec, window: Window):
    # Guard all readers, including the existing metric adapters, before materializing rows.
    lower, upper = window.start, window.stop
    if spec.kind == RecordKind.daily:
        lower = datetime.combine(window.first, time.min, UTC)
        upper = datetime.combine(window.end + timedelta(days=1), time.min, UTC)
    # Sleep and its summaries count on the local day the sleep ended (WAKE_DATED_TYPES).
    column = Raw.source_end_time if spec.name in WAKE_DATED_TYPES else Raw.source_start_time
    condition = (Raw.data_type == spec.name, column >= lower, column < upper)
    metric = next((metric for metric in METRICS if metric.data_type == spec.name), None)
    model = NORMALIZED.get(metric.read) if metric else None
    if model is not None and metric is not None:
        if metric.read == "steps":
            # Step readers now select current raw intervals. Bound that same candidate
            # set, including corrections whose retained normalized interval moved away.
            model = Raw
            bounds = (
                Raw.data_type == spec.name,
                Raw.source_end_time > lower,
                Raw.source_start_time < upper,
            )
        elif metric.read == "sleep":
            # Current wake dates, including missing starts; never retained old dates.
            model = Raw
            bounds = (
                Raw.data_type == spec.name,
                *sleep_wake_window(Raw.source_end_time, lower, upper),
            )
        else:
            # Weight presence reads current source timestamps, including invalid
            # corrections moved away from their preserved normalized timestamp.
            model = Raw
            bounds = condition
        count = await session.scalar(select(func.count()).select_from(model).where(*bounds))
    else:
        count = await session.scalar(select(func.count()).select_from(Raw).where(*condition))
    if (count or 0) > MAX_RECORDS:
        raise CoverageLimit("coverage_record_limit")
    if metric:
        streams, invalid = await google_streams(session, metric, window)
        exclusions = await metric_exclusions(session, metric, window)
        exclusions["invalid_shape"] = invalid
    else:
        streams = {}
        exclusions = await metric_exclusions(
            session,
            Metric(
                spec.name, spec.name, "raw_daily" if spec.kind == RecordKind.daily else "raw_sample"
            ),
            window,
        )
        if spec.time_filtered:
            # Only the source member. JSON `->` returns JSON,
            # so a boolean source keeps its own hash; scalar extraction would make it 0/1.
            source = Raw.raw_json.op("->", return_type=RawJSON)("dataSource")
            streams = await source_streams(
                session,
                select(source, column).where(*condition, raw_counted(data_types=(spec.name,))),
                spec,
                window,
            )
    values = set().union(*(stream.days for stream in streams.values()))
    if spec.name == "sleep":
        values = await sleep_value_days(session, window)
    regimes = [
        regime.model_dump(mode="json")
        for stream in streams.values()
        if (regime := source_regime(stream, window)) is not None
    ]
    return values, regimes, exclusions


async def daily_evidence_pieces(
    session: AsyncSession, spec: DataTypeSpec, window: Window, horizon: datetime | None
) -> dict[date, tuple[str, int | None, str | None]]:
    """Reduce only evidence-budget failures; retain daily results, never receipt lists.

    Every piece uses the full lineage/envelope reader. Splitting changes the read
    window, not the evidence rules or the per-read ceilings. A single-day failure
    or exhausted attempt budget refuses the whole report with the original code.

    Split on local dates so physical DST bounds and daily UTC coordinates are
    reconstructed by Window. The reader still selects every intersecting receipt,
    including continuation ancestors, using its civil envelope. Never apportion
    window totals: each day's count and reason come from daily_evidence itself.
    Successful pieces release ORM rows and timestamp histograms before another
    read; only the bounded map of scalar daily results survives.
    """
    pending = [(window.first, window.end)]
    results: dict[date, tuple[str, int | None, str | None]] = {}
    reads = 0
    failure = "coverage_evidence_limit"
    while pending:
        if reads >= MAX_EVIDENCE_PIECES:
            raise CoverageLimit(failure)
        first, last = pending.pop()
        piece = Window(first, last, window.today, window.zone)
        reads += 1
        try:
            attempts, times = await evidence(session, spec.name, piece)
        except CoverageLimit as exc:
            if (
                str(exc) not in {"coverage_receipt_limit", "coverage_evidence_limit"}
                or first == last
            ):
                raise
            failure = str(exc)
            middle = first + timedelta(days=(last - first).days // 2)
            pending.extend([(middle + timedelta(days=1), last), (first, middle)])
            continue
        day = first
        while day <= last:
            results[day] = daily_evidence(spec, day, piece, attempts, times, horizon)
            day += timedelta(days=1)
        del attempts, times
    return results


async def coverage_report(
    session: AsyncSession,
    specs: dict[str, DataTypeSpec],
    data_type: str | None = None,
    offset: int = 0,
    limit: int = 4,
    *,
    dates: CivilDateRange,
) -> Coverage:
    await plan_each_query(session)
    zone = ZoneInfo(dates.timezone)
    window = Window(dates.first, dates.last, datetime.now(zone).date(), zone)
    days = [dates.first + timedelta(days=i) for i in range((dates.last - dates.first).days + 1)]
    names = [data_type] if data_type else sorted(specs)
    # At most 62 type/day cells; each HTTP response stays within the card's 12 KiB budget.
    limit = min(limit, max(1, 62 // len(days)))
    selected = names[offset : offset + limit]
    types = []
    missing = unknown = incomplete = not_attempted = 0
    horizon = await receipt_horizon(session)
    for name in selected:
        spec = specs[name]
        states = await daily_evidence_pieces(session, spec, window, horizon)
        values, sources, exclusions = await presence(session, spec, window)
        entries = []
        for day in days:
            state, returned, reason = states[day]
            closed = day < window.today
            missing += closed and spec.time_filtered and day not in values
            unknown += closed and state == "unknown"
            incomplete += closed and state == "incomplete"
            not_attempted += closed and state == "not_attempted"
            entries.append(
                {
                    "date": day.isoformat(),
                    "has_value": day in values if spec.time_filtered else None,
                    "retrieval": state,
                    "returned_records": returned,
                    "reason": reason,
                    "closed": closed,
                }
            )
        types.append(
            {
                "data_type": name,
                "presence_basis": "metric_value"
                if any(m.data_type == name for m in METRICS)
                else "eligible_record",
                "days": entries,
                "source_regimes": sources,
                "exclusions": exclusions,
            }
        )
    report: Coverage = {
        "schema_version": "1",
        "start_date": dates.first.isoformat(),
        "end_date": dates.last.isoformat(),
        "timezone": dates.timezone,
        "types": types,
        "supported_data_types": sorted(specs),
        "summary": {
            "days_without_value": missing,
            "unknown_days": unknown,
            "incomplete_days": incomplete,
            "not_attempted_days": not_attempted,
            "basis": "type_days_in_page",
        },
        "receipts_since": horizon.isoformat() if horizon else None,
        "next_offset": offset + len(selected) if offset + len(selected) < len(names) else None,
        "note": "Değer varlığı çekim kanıtından ayrıdır. Kaynaklar birleştirilmez. "
        "Fişsiz geçmiş bilinmiyor; fişler başladıktan sonra hiç dokunulmayan gün "
        "çekilmedi demektir. Bugün kapalı gün değildir. "
        "Dönen kayıtlar dışlananları da içerir; sayılar ölçüm sıklığı değildir.",
    }
    return _bounded_report(report)


def _bounded_report(report: Coverage) -> Coverage:
    if (
        len(json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode())
        > COVERAGE_BUDGET_BYTES
    ):
        raise CoverageLimit("coverage_response_budget")
    return report
