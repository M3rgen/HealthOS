"""Pull the Garmin Connect Personal API into the archive's Garmin tables (phase 1).

Every pull is a set of GETs over a window of calendar days, one request per window chunk
(range kinds: an inclusive date range the endpoint accepts; day kinds: one request per
day). Each response is archived byte for byte in ``garmin_api_responses`` and its records
in ``garmin_raw_records``, in one transaction per request: a crash keeps both or neither.
A record is identified by its kind and key (the calendar day, or the activity id); the same
content fetched again adds an observation, a changed content is a new row and the earlier row
stays (readers take the last observed version). After a pull or a backfill the current records are
normalized into the shared normalized tables tagged ``provider = 'GARMIN'``
(app/services/providers/garmin_normalize.py); nothing Google-shaped (``raw_health_data``, sightings,
identities) is written.

* ``run()``: the scheduled incremental pull, the last ``GARMIN_LOOKBACK_DAYS`` days.
* ``backfill(since, until)``: chunks the range per kind, keeps a checkpoint per kind in
  ``garmin_pull_state`` (``backfill_next``) and resumes an interrupted run with the same
  bounds; ``dry_run`` only counts requests.
* ``status()`` and ``daily()``: read-only projections for ``GET /api/garmin/status`` and
  ``GET /api/garmin/daily``; ``daily`` says, per metric, whether Garmin is the primary or a
  secondary source under the precedence policy (app/services/identities/source_precedence.py).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Self

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.fetch_window import FetchWindow
from app.core.ingestion_stats import NormalizeStats, StoreStats
from app.core.logging import event
from app.core.normalization import payload_hash as record_digest  # noqa: F401 - public import
from app.core.provenance import new_context_id, provenance
from app.core.timeutil import as_utc
from app.db.contracts import OwnershipLost, RunOwnership, TransactionSessions
from app.garmin.client import GarminClient
from app.models.garmin import GarminApiResponse, GarminPullState, GarminRawRecord
from app.schemas.status import GarminDaily, GarminStatus
from app.services.identities.source_precedence import Policy, category

from .garmin_checkpoints import Checkpoints, Fenced
from .garmin_current import newest_records
from .garmin_ingestion import GarminPage, GarminSource
from .garmin_kinds import (  # noqa: F401 - existing public imports
    ACTIVITY_PAGE,
    BY_NAME,
    KIND_NAMES,
    KINDS,
    Kind,
    iso,
)
from .garmin_normalization import normalize as normalize_archive
from .garmin_normalization import read_status as normalization_status
from .ingestion_contracts import FetchControls, IngestionSource, ingest_pages

logger = logging.getLogger(__name__)

# The scheduled pull stays out of the nightly window (02:00-03:59 UTC: Google reconciliation
# at 02:00, identity reconciler at 02:30, Sunday reconciliation at 03:00, backup at 03:30).
QUIET_HOURS = (2, 3)
OWNERSHIP_LOST = "ownership_lost"


def day_window(start: date, end: date) -> FetchWindow:
    """Convert the public inclusive Garmin dates to the shared half-open UTC interval."""
    return FetchWindow(
        datetime.combine(start, datetime.min.time(), UTC),
        datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC),
    )


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
            "from": iso(self.start),
            "to": iso(self.end),
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


class GarminOwnershipRequired(RuntimeError):
    """A write operation was requested without an explicit run owner."""


class GarminPull:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker,
        *,
        client: GarminClient | None = None,
        source: IngestionSource[GarminPage] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.clock = clock or (lambda: datetime.now(UTC))
        self._ownership: RunOwnership | None = None
        self.checkpoints = Checkpoints(self.clock)
        self.backend: GarminSource | None = None
        if source is None:
            self.backend = GarminSource(settings, sessions, client=client, clock=self.clock)
            self.source: IngestionSource[GarminPage] = self.backend
        else:
            self.source = source

    def owned_by(self, ownership: RunOwnership) -> Self:
        """Serialize this pull's runs through ``ownership``; ``app.runtime`` always does.

        Required before run/backfill (including dry runs) or normalization.
        """
        self._ownership = ownership
        return self

    @property
    def enabled(self) -> bool:
        return self.settings.garmin_enabled

    @property
    def client(self) -> GarminClient:
        if self.backend is None:
            raise RuntimeError("The injected source owns its client")
        return self.backend.client

    async def aclose(self) -> None:
        if self.backend is not None:
            await self.backend.aclose()

    # -- pulls -------------------------------------------------------------------------

    async def run(self, kinds: Sequence[str] | None = None) -> PullSummary:
        """The incremental pull: the last ``GARMIN_LOOKBACK_DAYS`` days up to today (UTC)."""
        self._require_ownership()
        end = self.clock().date()
        start = end - timedelta(days=self.settings.garmin_lookback_days)
        summary = PullSummary(new_context_id("garmin-pull"), start, end)
        started = self.clock()
        for kind in self._kinds(kinds):
            result = KindResult()
            summary.kinds[kind.name] = result
            try:
                async with self._owned(kind.name) as owned:
                    try:
                        for chunk_start, chunk_end in plan(kind, start, end):
                            await self._check(owned)
                            await self._fetch(
                                owned, kind, chunk_start, chunk_end, summary.context_id, result
                            )
                        await self.checkpoints.mark(owned, kind.name, success=True)
                    except OwnershipLost:
                        raise
                    except Exception as exc:
                        result.error = await self.checkpoints.record_failure(
                            owned, kind.name, exc, "GARMIN_PULL_KIND_FAILED"
                        )
            except OwnershipLost:
                # A replacement owner may be running; nothing here may be written anymore.
                result.error = OWNERSHIP_LOST
                event(logger, "GARMIN_PULL_OWNERSHIP_LOST", kind=kind.name)
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
        """Normalize archived records through the source's public stage, one run at a time."""
        async with self._owned("normalize") as owned:
            return await normalize_archive(
                self.source, day_window(start, end), owned, clock=self.clock, seen_since=seen_since
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
        self._require_ownership()
        until = until or self.clock().date()
        if until < since:
            raise ValueError("--until must not be before --since")
        context_id = new_context_id("garmin-backfill")
        report: dict[str, Any] = {
            "from": iso(since),
            "to": iso(until),
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
            await self._backfill_kind(kind, since, until, context_id, entry)
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

    async def _backfill_kind(
        self, kind: Kind, since: date, until: date, context_id: str, entry: dict[str, Any]
    ) -> None:
        """One kind's chunks under its ownership: the bounds and the checkpoint stay this run's."""
        result = KindResult()
        try:
            async with self._owned(kind.name) as owned:
                try:
                    resume = await self.checkpoints.checkpoint(owned, kind.name, since, until)
                    if resume is None:
                        entry["complete"] = True
                        entry["requests_planned"] = 0
                        return
                    if resume != since:
                        entry["resumed_from"] = iso(resume)
                    for chunk_start, chunk_end in windows(resume, until, kind.max_days):
                        await self._check(owned)
                        await self._fetch(owned, kind, chunk_start, chunk_end, context_id, result)
                        await self.checkpoints.advance(owned, kind.name, since, until, chunk_end)
                    await self.checkpoints.mark(owned, kind.name, success=True)
                    entry["complete"] = True
                except OwnershipLost:
                    raise
                except Exception as exc:
                    entry["error"] = await self.checkpoints.record_failure(
                        owned, kind.name, exc, "GARMIN_BACKFILL_KIND_FAILED"
                    )
        except OwnershipLost:
            # A replacement owner may be resuming; its checkpoint is left alone.
            entry["error"] = OWNERSHIP_LOST
            event(logger, "GARMIN_BACKFILL_OWNERSHIP_LOST", kind=kind.name)
        entry.update(
            requests=result.requests, records=result.records, new_records=result.new_records
        )

    def _kinds(self, names: Sequence[str] | None) -> list[Kind]:
        if not names:
            return list(KINDS)
        unknown = sorted(set(names) - set(BY_NAME))
        if unknown:
            raise ValueError(f"unknown Garmin kind(s): {', '.join(unknown)}")
        return [BY_NAME[name] for name in names]

    async def _fetch(
        self,
        owned: TransactionSessions,
        kind: Kind,
        start: date,
        end: date,
        context_id: str,
        result: KindResult,
    ) -> None:
        """Fetch without a transaction, then publish each page on the owner connection.

        The pre-request check cannot fence a response that outlives its lock. The page
        transaction must use the same capability as the checkpoint; on ownership loss
        ingest_pages hands only HTTP evidence to keep_after and reports no progress.
        """

        def attempted() -> None:
            result.requests += 1

        async def commit(page: GarminPage) -> StoreStats:
            async with owned.begin() as session:
                return await self.source.store_raw(session, kind, page)

        with provenance(context_id=context_id):
            async for page, stats in ingest_pages(
                self.source,
                kind,
                day_window(start, end),
                commit,
                controls=FetchControls(on_request=attempted),
            ):
                result.records += len(page.data_points)
                result.new_records += stats.inserted
                result.unstorable += stats.rejected

    # -- checkpoints -------------------------------------------------------------------

    @asynccontextmanager
    async def _owned(self, key: str) -> AsyncIterator[TransactionSessions]:
        async with self._require_ownership()(key) as held:
            yield Fenced(held)

    def _require_ownership(self) -> RunOwnership:
        if self._ownership is None:
            raise GarminOwnershipRequired("GarminPull requires explicit run ownership")
        return self._ownership

    @staticmethod
    async def _check(sessions: TransactionSessions) -> None:
        """Stop before the next request once ownership is lost (no transaction spans I/O)."""
        async with sessions.begin():
            pass

    # -- projections -------------------------------------------------------------------

    async def status(self) -> GarminStatus:
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
            normalization = await normalization_status(session)
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
                    "since": iso(state.backfill_since),
                    "until": iso(state.backfill_until) if state.backfill_until else None,
                    "next": iso(state.backfill_next) if state.backfill_next else None,
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
            "normalization": normalization,
        }

    async def daily(self, start: date, end: date) -> GarminDaily:
        """A read-only per-day summary from the newest Garmin record of each kind and day."""
        wanted = (
            "steps",
            "daily-summaries",
            "heart-rate",
            "sleep",
            "sleep-summaries",
            "hrv-summaries",
            "hrv",
            "spo2",
            "stress",
        )
        async with self.sessions() as session:
            records = await newest_records(session, wanted, start, end)
        latest = {
            (kind, key): body for kind, values in records.items() for key, body in values.items()
        }
        days = sorted({key for _, key in latest})
        policy = Policy.from_settings(self.settings)
        return {
            "source": "garmin",
            "from": iso(start),
            "to": iso(end),
            # Under the precedence policy: is Garmin shown first for the metric, or kept as an
            # alternative to the primary source (app/services/identities/source_precedence.py)?
            "precedence": {
                metric: policy.priority("GARMIN", category(metric)) for metric in DAILY_METRICS
            },
            "policy": policy.as_dict(),
            "days": [_day(day, {kind: latest.get((kind, day)) for kind in wanted}) for day in days],
        }


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
    steps, summary, spo2 = (records.get(name) for name in ("steps", "daily-summaries", "spo2"))
    # Sleep and HRV fall back by whole record, even when a present primary has no value.
    sleep = records.get("sleep") or records.get("sleep-summaries")
    hrv = records.get("hrv-summaries") or _get(records.get("hrv"), "hrvSummary")
    resting = _number(_get(summary, "heartRate", "restingValue"))
    if resting is None:
        resting = _number(_get(records.get("heart-rate"), "restingHeartRate"))
    stress = _number(_get(records.get("stress"), "values", "overallStressLevel"))
    if stress is None:
        stress = _number(_get(summary, "stress", "avgLevel"))
    return {
        "date": day,
        "steps": _number(_get(steps, "totalSteps")),
        "distance_m": _number(_get(steps, "totalDistance")),
        "resting_heart_rate": resting,
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
        "stress_average": stress,
        "hrv_last_night_average": _number(_get(hrv, "lastNightAvg")),
        "spo2_average": _number(_get(spo2, "averageSpO2")),
    }
