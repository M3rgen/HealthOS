"""Pull the Garmin Connect Personal API into the archive's Garmin tables (phase 1).

Every pull is a set of GETs over a window of calendar days, one request per window chunk
(range kinds: an inclusive date range the endpoint accepts; day kinds: one request per
day). Each response is archived byte for byte in ``garmin_api_responses`` and its records
in ``garmin_raw_records``, in one transaction per request: a crash keeps both or neither.
A record is identified by its kind and key (the calendar day, or the activity id); the same
content fetched again adds nothing, a changed content is a new row and the earlier row
stays (readers take the newest). After a pull or a backfill the window's newest records are
normalized into the shared normalized tables tagged ``provider = 'GARMIN'``
(app/services/garmin_normalize.py); nothing Google-shaped (``raw_health_data``, sightings,
identities) is written.

* ``run()``: the scheduled incremental pull, the last ``GARMIN_LOOKBACK_DAYS`` days.
* ``backfill(since, until)``: chunks the range per kind, keeps a checkpoint per kind in
  ``garmin_pull_state`` (``backfill_next``) and resumes an interrupted run with the same
  bounds; ``dry_run`` only counts requests.
* ``status()`` and ``daily()``: read-only projections for ``GET /api/garmin/status`` and
  ``GET /api/garmin/daily``; ``daily`` says, per metric, whether Garmin is the primary or a
  secondary source under the precedence policy (app/services/source_precedence.py).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Table, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import error_diagnostics, event
from app.core.provenance import new_context_id
from app.core.timeutil import as_utc
from app.garmin.client import GarminClient, GarminError, GarminExchange
from app.google_health.jsonb import jsonb_storable
from app.models.garmin import GarminApiResponse, GarminPullState, GarminRawRecord
from app.parsers.common import canonical_json
from app.services.garmin_normalize import NormalizeStats, normalize_window
from app.services.source_precedence import Policy, category

logger = logging.getLogger(__name__)

# Activities per search page (Garmin's default is 20; 100 keeps the request count low).
ACTIVITY_PAGE = 100
# The scheduled pull stays out of the nightly window (02:00-03:59 UTC: Google reconciliation
# at 02:00, identity reconciler at 02:30, Sunday reconciliation at 03:00, backup at 03:30).
QUIET_HOURS = (2, 3)


@dataclass(frozen=True)
class Kind:
    """One Garmin endpoint family and how its records are keyed.

    ``mode``: ``range`` (one request per chunk of at most ``max_days`` inclusive days),
    ``day`` (one request per calendar day) or ``search`` (paged activity search over a
    range). ``request(start, end)`` gives the path and query parameters; ``records(body)``
    the records of a response; ``key(record)`` the record's calendar day or id (``None``:
    fall back to the requested day, else to the content digest).
    """

    name: str
    mode: str
    max_days: int
    request: Callable[[date, date], tuple[str, dict[str, Any]]]
    records: Callable[[Any], list]
    key: Callable[[Any], str | None]


def _iso(day: date) -> str:
    return day.isoformat()


def _list(body: Any) -> list:
    return list(body) if isinstance(body, list) else []


def _field(body: Any, name: str) -> list:
    return _list(body.get(name)) if isinstance(body, dict) else []


def _single(body: Any) -> list:
    return [body] if isinstance(body, dict) and body else []


def _calendar_date(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    for path in (("calendarDate",), ("date",), ("nextSleepNeed", "calendarDate")):
        value: Any = record
        for name in path:
            value = value.get(name) if isinstance(value, dict) else None
        if isinstance(value, str) and value:
            return value[:64]
    return None


def _activity_id(record: Any) -> str | None:
    if isinstance(record, dict) and isinstance(record.get("activityId"), int | str):
        return str(record["activityId"])[:64]
    return None


KINDS: tuple[Kind, ...] = (
    Kind(
        "daily-summaries",
        "range",
        90,
        lambda s, e: (
            "/usersummary-service/v2/dailysummaries",
            {"startDate": _iso(s), "endDate": _iso(e)},
        ),
        lambda body: _field(body, "data"),
        _calendar_date,
    ),
    Kind(
        "steps",
        "range",
        28,
        lambda s, e: (f"/usersummary-service/stats/steps/daily/{_iso(s)}/{_iso(e)}", {}),
        _list,
        _calendar_date,
    ),
    Kind(
        "stress",
        "range",
        28,
        lambda s, e: (f"/usersummary-service/stats/stress/daily/{_iso(s)}/{_iso(e)}", {}),
        _list,
        _calendar_date,
    ),
    Kind(
        "body-battery",
        "range",
        28,
        lambda s, e: (
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": _iso(s), "endDate": _iso(e)},
        ),
        _list,
        _calendar_date,
    ),
    Kind(
        "sleep-summaries",
        "range",
        31,
        lambda s, e: (
            "/sleep-service/sleep/dailySleepSummariesByDate",
            {"startDate": _iso(s), "endDate": _iso(e)},
        ),
        _list,
        _calendar_date,
    ),
    Kind(
        "hrv-summaries",
        "range",
        28,
        lambda s, e: (f"/hrv-service/hrv/daily/{_iso(s)}/{_iso(e)}", {}),
        lambda body: _field(body, "hrvSummaries"),
        _calendar_date,
    ),
    Kind(
        "heart-rate",
        "day",
        1,
        lambda s, e: ("/wellness-service/wellness/dailyHeartRate", {"date": _iso(s)}),
        _single,
        lambda record: None,
    ),
    Kind(
        "spo2",
        "day",
        1,
        lambda s, e: (f"/wellness-service/wellness/daily/spo2acclimation/{_iso(s)}", {}),
        _single,
        lambda record: None,
    ),
    Kind(
        "sleep",
        "day",
        1,
        lambda s, e: ("/sleep-service/sleep/dailySleep", {"date": _iso(s)}),
        _single,
        lambda record: None,
    ),
    Kind(
        "hrv",
        "day",
        1,
        lambda s, e: (f"/hrv-service/hrv/{_iso(s)}", {}),
        _single,
        lambda record: None,
    ),
    Kind(
        "activities",
        "search",
        90,
        lambda s, e: (
            "/activitylist-service/activities/search/activities",
            {"startDate": _iso(s), "endDate": _iso(e), "limit": ACTIVITY_PAGE},
        ),
        _list,
        _activity_id,
    ),
)
KIND_NAMES = tuple(kind.name for kind in KINDS)
BY_NAME = {kind.name: kind for kind in KINDS}


def windows(start: date, end: date, max_days: int) -> Iterator[tuple[date, date]]:
    """Inclusive chunks of at most ``max_days`` days covering ``start``..``end``."""
    if end < start:
        return
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=max_days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def plan(kind: Kind, start: date, end: date) -> list[tuple[date, date]]:
    """The requests a pull of ``kind`` over ``start``..``end`` makes (one per chunk; a search
    may page further)."""
    return list(windows(start, end, kind.max_days))


def cron_fields(interval_minutes: int) -> dict[str, str]:
    """APScheduler cron fields for a pull every ``interval_minutes`` outside QUIET_HOURS.

    Intervals of an hour or more run at a fixed minute past every n-th hour; shorter ones
    every n minutes of every hour; both skip the quiet hours. The minute is 10, away from
    the freshness refresh at :00 and the identity reconciler at :30.
    """
    hours = [hour for hour in range(24) if hour not in QUIET_HOURS]
    if interval_minutes >= 60:
        step = max(1, min(24, interval_minutes // 60))
        chosen = [hour for hour in range(0, 24, step) if hour not in QUIET_HOURS]
        return {"hour": ",".join(str(hour) for hour in chosen), "minute": "10"}
    return {"hour": ",".join(str(hour) for hour in hours), "minute": f"*/{interval_minutes}"}


@dataclass
class KindResult:
    requests: int = 0
    records: int = 0
    new_records: int = 0
    unstorable: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "records": self.records,
            "new_records": self.new_records,
            "unstorable": self.unstorable,
            "error": self.error,
        }


@dataclass
class PullSummary:
    context_id: str
    start: date
    end: date
    kinds: dict[str, KindResult] = field(default_factory=dict)
    normalized: NormalizeStats | None = None

    @property
    def failed(self) -> bool:
        return any(result.error for result in self.kinds.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "from": _iso(self.start),
            "to": _iso(self.end),
            "requests": sum(r.requests for r in self.kinds.values()),
            "records": sum(r.records for r in self.kinds.values()),
            "new_records": sum(r.new_records for r in self.kinds.values()),
            "kinds": {name: result.as_dict() for name, result in self.kinds.items()},
            "normalized": self.normalized.as_dict() if self.normalized else None,
        }


# The metrics ``daily()`` reports, as the dashboard names them (for the precedence line).
DAILY_METRICS = (
    "steps",
    "distance",
    "resting_bpm",
    "sleep_seconds",
    "hrv_ms",
    "spo2",
    "stress",
    "body_battery",
)


def record_digest(record: Any) -> str:
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


class GarminPull:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        *,
        client: GarminClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self._client = client
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def enabled(self) -> bool:
        return self.settings.garmin_enabled

    @property
    def client(self) -> GarminClient:
        if self._client is None:
            self._client = GarminClient(self.settings.garmin_api_key_file)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- pulls -------------------------------------------------------------------------

    async def run(self, kinds: Sequence[str] | None = None) -> PullSummary:
        """The incremental pull: the last ``GARMIN_LOOKBACK_DAYS`` days up to today (UTC)."""
        end = self.clock().date()
        start = end - timedelta(days=self.settings.garmin_lookback_days)
        summary = PullSummary(new_context_id("garmin-pull"), start, end)
        started = self.clock()
        for kind in self._kinds(kinds):
            result = KindResult()
            summary.kinds[kind.name] = result
            try:
                for chunk_start, chunk_end in plan(kind, start, end):
                    await self._fetch(kind, chunk_start, chunk_end, summary.context_id, result)
                await self._mark(kind.name, success=True)
            except GarminError as exc:
                result.error = str(exc)
                await self._mark(kind.name, success=False, error=str(exc))
                event(logger, "GARMIN_PULL_KIND_FAILED", kind=kind.name, error=str(exc))
            except Exception as exc:
                diagnostics = error_diagnostics(exc)
                result.error = diagnostics.get("error_type", type(exc).__name__)
                await self._mark(kind.name, success=False, error=result.error)
                event(logger, "GARMIN_PULL_KIND_FAILED", kind=kind.name, **diagnostics)
        summary.normalized = await self.normalize(start, end, seen_since=started)
        event(
            logger,
            "GARMIN_PULL_COMPLETED",
            requests=summary.as_dict()["requests"],
            new_records=summary.as_dict()["new_records"],
            normalized=summary.normalized.inserted + summary.normalized.updated,
            failed_kinds=sorted(name for name, r in summary.kinds.items() if r.error),
        )
        return summary

    async def normalize(
        self, start: date, end: date, *, seen_since: datetime | None = None
    ) -> NormalizeStats:
        """Normalize the archive's newest records of ``start``..``end`` (idempotent)."""
        async with self.sessions.begin() as session:
            return await normalize_window(
                session,
                start,
                end,
                device=self.settings.garmin_device_name,
                seen_since=seen_since,
            )

    async def backfill(
        self,
        since: date,
        until: date | None = None,
        *,
        kinds: Sequence[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Pull ``since``..``until`` (default today) kind by kind with a checkpoint per kind.

        A kind whose stored checkpoint has the same bounds resumes from ``backfill_next``;
        one that already finished these bounds is skipped. Other bounds start over.
        """
        until = until or self.clock().date()
        if until < since:
            raise ValueError("--until must not be before --since")
        context_id = new_context_id("garmin-backfill")
        report: dict[str, Any] = {
            "from": _iso(since),
            "to": _iso(until),
            "dry_run": dry_run,
            "kinds": {},
            "normalized": None,
        }
        for kind in self._kinds(kinds):
            chunks = plan(kind, since, until)
            entry: dict[str, Any] = {
                "days": (until - since).days + 1,
                "requests_planned": len(chunks),
                "requests": 0,
                "records": 0,
                "new_records": 0,
                "resumed_from": None,
                "complete": False,
                "error": None,
            }
            report["kinds"][kind.name] = entry
            if dry_run:
                continue
            resume = await self._checkpoint(kind.name, since, until)
            if resume is None:
                entry["complete"] = True
                entry["requests_planned"] = 0
                continue
            if resume != since:
                entry["resumed_from"] = _iso(resume)
            result = KindResult()
            try:
                for chunk_start, chunk_end in windows(resume, until, kind.max_days):
                    await self._fetch(kind, chunk_start, chunk_end, context_id, result)
                    await self._advance(kind.name, chunk_end, until)
                await self._mark(kind.name, success=True)
                entry["complete"] = True
            except GarminError as exc:
                entry["error"] = str(exc)
                await self._mark(kind.name, success=False, error=str(exc))
                event(logger, "GARMIN_BACKFILL_KIND_FAILED", kind=kind.name, error=str(exc))
            except Exception as exc:
                diagnostics = error_diagnostics(exc)
                entry["error"] = diagnostics.get("error_type", type(exc).__name__)
                await self._mark(kind.name, success=False, error=entry["error"])
                event(logger, "GARMIN_BACKFILL_KIND_FAILED", kind=kind.name, **diagnostics)
            entry.update(
                requests=result.requests, records=result.records, new_records=result.new_records
            )
        report["failed"] = any(entry["error"] for entry in report["kinds"].values())
        if not dry_run:
            # Once, over the whole window (an interrupted run normalizes what it fetched when
            # it is resumed with the same bounds): one pass reads each day's kinds together.
            # Every archived activity that begins in the window is read, whenever it was first
            # archived (``seen_since=None``): a backfill over an archive filled before
            # normalization existed brings its activities into ``exercise_sessions`` too. The
            # periodic ``run()`` keeps reading only the activities its own pull archived.
            report["normalized"] = (await self.normalize(since, until)).as_dict()
        return report

    def _kinds(self, names: Sequence[str] | None) -> list[Kind]:
        if not names:
            return list(KINDS)
        unknown = sorted(set(names) - set(BY_NAME))
        if unknown:
            raise ValueError(f"unknown Garmin kind(s): {', '.join(unknown)}")
        return [BY_NAME[name] for name in names]

    async def _fetch(
        self, kind: Kind, start: date, end: date, context_id: str, result: KindResult
    ) -> None:
        path, params = kind.request(start, end)
        page = 0
        while True:
            if kind.mode == "search":
                params = {**params, "start": page * ACTIVITY_PAGE}
            result.requests += 1  # attempted; a failure is reported through ``error``
            exchanges: list[GarminExchange] = []
            try:
                response = await self.client.get(
                    path, params, kind=kind.name, context_id=context_id, exchanges=exchanges
                )
            except GarminError:
                # The error responses (400, a final 429/5xx, a refused token) are evidence too.
                if exchanges:
                    async with self.sessions.begin() as session:
                        await _insert_exchanges(session, exchanges)
                raise
            records = kind.records(response.json)
            fallback = _iso(start) if kind.mode == "day" else None
            new, unstorable = await self._store(kind, records, fallback, response.exchanges)
            result.records += len(records)
            result.new_records += new
            result.unstorable += unstorable
            if kind.mode != "search" or len(records) < ACTIVITY_PAGE:
                return
            page += 1

    async def _store(
        self,
        kind: Kind,
        records: list,
        fallback_key: str | None,
        exchanges: list[GarminExchange],
    ) -> tuple[int, int]:
        """Archive the exchanges and the records of one response in one transaction.

        Returns (records newly stored, records jsonb cannot hold). An unstorable record is
        counted and left out of ``garmin_raw_records``; its bytes stay in the response row.
        """
        from app.services.storage import dialect_insert

        now = self.clock()
        async with self.sessions.begin() as session:
            response_id = await _insert_exchanges(session, exchanges)
            rows: dict[tuple[str, str], dict[str, Any]] = {}
            unstorable = 0
            for record in records:
                if not jsonb_storable(record):
                    unstorable += 1
                    continue
                digest = record_digest(record)
                key = kind.key(record) or fallback_key or digest[:32]
                rows.setdefault(
                    (key, digest),
                    {
                        "kind": kind.name,
                        "key": key,
                        "body": record,
                        "body_sha256": digest,
                        "response_id": response_id,
                        "first_seen_at": now,
                    },
                )
            if unstorable:
                event(logger, "GARMIN_RECORDS_UNSTORABLE", kind=kind.name, count=unstorable)
            if not rows:
                return 0, unstorable
            keys = sorted({key for key, _ in rows})
            existing = set(
                (
                    await session.execute(
                        select(GarminRawRecord.key, GarminRawRecord.body_sha256).where(
                            GarminRawRecord.kind == kind.name, GarminRawRecord.key.in_(keys)
                        )
                    )
                ).tuples()
            )
            new_rows = [row for pair, row in rows.items() if pair not in existing]
            if new_rows:
                statement = dialect_insert(session, GarminRawRecord.__table__)
                await session.execute(
                    statement.on_conflict_do_nothing(index_elements=["kind", "key", "body_sha256"]),
                    new_rows,
                )
            return len(new_rows), unstorable

    # -- checkpoints -------------------------------------------------------------------

    async def _state(self, session: AsyncSession, kind: str) -> GarminPullState:
        state = await session.get(GarminPullState, kind)
        if state is None:
            state = GarminPullState(kind=kind)
            session.add(state)
        return state

    async def _mark(self, kind: str, *, success: bool, error: str | None = None) -> None:
        now = self.clock()
        async with self.sessions.begin() as session:
            state = await self._state(session, kind)
            state.last_attempt_at = now
            if success:
                state.last_success_at = now
            else:
                state.last_error = (error or "unknown")[:2000]
                state.last_error_at = now

    async def _checkpoint(self, kind: str, since: date, until: date) -> date | None:
        """The day to start ``kind``'s backfill from: the stored ``backfill_next`` when the
        bounds match (``None`` when those bounds already finished), else ``since`` anew."""
        async with self.sessions.begin() as session:
            state = await self._state(session, kind)
            if state.backfill_since == since and state.backfill_until == until:
                return state.backfill_next
            state.backfill_since = since
            state.backfill_until = until
            state.backfill_next = since
            return since

    async def _advance(self, kind: str, done_through: date, until: date) -> None:
        async with self.sessions.begin() as session:
            state = await self._state(session, kind)
            following = done_through + timedelta(days=1)
            state.backfill_next = following if following <= until else None

    # -- projections -------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        async with self.sessions() as session:
            counts = {
                kind: (total, newest)
                for kind, total, newest in (
                    await session.execute(
                        select(
                            GarminRawRecord.kind,
                            func.count(GarminRawRecord.id),
                            func.max(GarminRawRecord.key),
                        ).group_by(GarminRawRecord.kind)
                    )
                ).tuples()
            }
            responses = await session.scalar(select(func.count(GarminApiResponse.id))) or 0
            states = {
                state.kind: state for state in (await session.scalars(select(GarminPullState)))
            }
        kinds: dict[str, Any] = {}
        for name in KIND_NAMES:
            total, newest = counts.get(name, (0, None))
            state = states.get(name)
            kinds[name] = {
                "records": total,
                "newest_key": newest,
                "last_success_at": _iso_time(state.last_success_at if state else None),
                "last_error": state.last_error if state else None,
                "last_error_at": _iso_time(state.last_error_at if state else None),
                # The kind's last run failed (its error is newer than its last success).
                "failing": _failing(state),
                "backfill": None
                if state is None or state.backfill_since is None
                else {
                    "since": _iso(state.backfill_since),
                    "until": _iso(state.backfill_until) if state.backfill_until else None,
                    "next": _iso(state.backfill_next) if state.backfill_next else None,
                    "complete": state.backfill_next is None,
                },
            }
        successes = [s.last_success_at for s in states.values() if s.last_success_at]
        failures = [s for s in states.values() if _failing(s)]
        latest_failure = max(failures, key=lambda s: as_utc(s.last_error_at)) if failures else None
        return {
            "enabled": self.enabled,
            "key_file_present": self.settings.garmin_api_key_file.expanduser().is_file(),
            "pull_interval_minutes": self.settings.garmin_pull_interval_minutes,
            "lookback_days": self.settings.garmin_lookback_days,
            "last_success_at": _iso_time(max(successes, key=as_utc)) if successes else None,
            # The newest error among the kinds whose last run failed; None when all succeed.
            "last_error": latest_failure.last_error if latest_failure else None,
            "last_error_at": _iso_time(latest_failure.last_error_at) if latest_failure else None,
            "failing_kinds": sorted(name for name, kind in kinds.items() if kind["failing"]),
            "raw_records": sum(total for total, _ in counts.values()),
            "responses": responses,
            "kinds": kinds,
        }

    async def daily(self, start: date, end: date) -> dict[str, Any]:
        """A read-only per-day summary from the newest Garmin record of each kind and day."""
        wanted = ("steps", "daily-summaries", "sleep", "hrv-summaries", "spo2")
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(GarminRawRecord.kind, GarminRawRecord.key, GarminRawRecord.body)
                    .where(
                        GarminRawRecord.kind.in_(wanted),
                        GarminRawRecord.key >= _iso(start),
                        GarminRawRecord.key <= _iso(end),
                    )
                    .order_by(GarminRawRecord.id)
                )
            ).tuples()
        latest: dict[tuple[str, str], Any] = {}
        for kind, key, body in rows:
            latest[(kind, key)] = body  # ascending id: the newest version wins
        days = sorted({key for _, key in latest})
        policy = Policy.from_settings(self.settings)
        return {
            "source": "garmin",
            "from": _iso(start),
            "to": _iso(end),
            # Under the precedence policy: is Garmin shown first for the metric, or kept as an
            # alternative to the primary source (app/services/source_precedence.py)?
            "precedence": {
                metric: policy.priority("GARMIN", category(metric)) for metric in DAILY_METRICS
            },
            "policy": policy.as_dict(),
            "days": [_day(day, {kind: latest.get((kind, day)) for kind in wanted}) for day in days],
        }


async def _insert_exchanges(session: AsyncSession, exchanges: list[GarminExchange]) -> int | None:
    """Append the exchanges to ``garmin_api_responses``; the id of the last one."""
    if not exchanges:
        return None
    table = cast(Table, GarminApiResponse.__table__)
    result = await session.execute(
        insert(table).returning(table.c.id, sort_by_parameter_order=True),
        [exchange.row() for exchange in exchanges],
    )
    ids = list(result.scalars().all())
    return ids[-1] if ids else None


def _failing(state: GarminPullState | None) -> bool:
    if state is None or state.last_error_at is None:
        return False
    if state.last_success_at is None:
        return True
    return as_utc(state.last_error_at) > as_utc(state.last_success_at)


def _iso_time(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value is not None else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _get(record: Any, *path: str) -> Any:
    value = record
    for name in path:
        value = value.get(name) if isinstance(value, dict) else None
    return value


def _day(day: str, records: dict[str, Any]) -> dict[str, Any]:
    steps, summary, sleep, hrv, spo2 = (
        records.get(name) for name in ("steps", "daily-summaries", "sleep", "hrv-summaries", "spo2")
    )
    return {
        "date": day,
        "steps": _number(_get(steps, "totalSteps")),
        "distance_m": _number(_get(steps, "totalDistance")),
        "resting_heart_rate": _number(_get(summary, "heartRate", "restingValue")),
        "sleep_seconds": _number(_get(sleep, "sleepTimeSeconds")),
        "sleep_stages_seconds": {
            "deep": _number(_get(sleep, "deepSleepSeconds")),
            "light": _number(_get(sleep, "lightSleepSeconds")),
            "rem": _number(_get(sleep, "remSleepSeconds")),
            "awake": _number(_get(sleep, "awakeSleepSeconds")),
        },
        "sleep_score": _number(_get(sleep, "sleepScores", "overall", "value")),
        "body_battery": {
            "max": _number(_get(summary, "bodyBattery", "maxValue")),
            "min": _number(_get(summary, "bodyBattery", "minValue")),
            "latest": _number(_get(summary, "bodyBattery", "latestValue")),
            "charged": _number(_get(summary, "bodyBattery", "chargedValue")),
            "drained": _number(_get(summary, "bodyBattery", "drainedValue")),
        },
        "stress_average": _number(_get(summary, "stress", "avgLevel")),
        "hrv_last_night_average": _number(_get(hrv, "lastNightAvg")),
        "spo2_average": _number(_get(spo2, "averageSpO2")),
    }
