"""Catch every Google data type up on a timer, independent of webhooks.

A notification only speeds things up. On 15 November 2026 Google stopped notifying for hours
while it held new records, and nothing but the nightly reconciliation fetched them. Here each
data type's incremental window (``IncrementalSync.catch_up``: the last successful sync minus
the type's safety window, up to now) is fetched on a schedule: the frequent types every
CATCH_UP_FREQUENT_MINUTES, the others every CATCH_UP_OTHER_MINUTES, staggered over the
interval and one type at a time (a small server with one disk).

No request goes out when a complete fetch covering the window, up to the recent past, began
recently: a notification's own catch-up, a reconciliation or an earlier round. Recently is the
type's interval, but at least NOTIFIED_COVER (45 minutes): a notification catches its type up at
most every 30 minutes, so with notifications every 14 minutes (the rate before the incident)
their catch-ups recur every 42 minutes. While a type's notifications arrive at least every
15 minutes the timer only reads the receipts and adds no request; when they stop, a frequent
type is fetched every 45 minutes. A type whose run ownership is taken (a reconciliation window,
a backfill, a notification) is skipped without waiting and tried again on the next tick; nothing
queues. A reconciliation calls ``between_slices`` after each window of a type, so a due catch-up
of that very type runs between two windows instead of after the whole pass.

Failures never become a storm. Credentials that need a new consent, and transient Google
failures (429, 5xx, network), pause every type with a doubling delay; any other failure backs
off its own type. The credentials are checked before a fetch changes any sync state, so a lost
Google connection leaves the sync states alone. One log line opens and one closes each pause.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.config import Settings
from app.core.ingestion_stats import StoreStats
from app.core.logging import event
from app.core.oauth_keys import CredentialStoreError
from app.core.provenance import new_context_id, provenance
from app.core.webhook_types import Reason
from app.google_health.errors import GoogleHealthError
from app.google_health.oauth import OAuthError
from app.google_health.pagination import PaginationPaused
from app.services.synchronization.reconciliation_reports import reporting
from app.sync.catch_up import CATCH_UP_INTERVAL, TypeBusy
from app.sync.errors import database_conflict, error_code, is_transient
from app.sync.incremental import IncrementalSync

logger = logging.getLogger(__name__)

# The scheduler job's period: due types are looked at this often.
TICK = timedelta(minutes=5)
# The notification catch-up's own limit plus one gap between notifications.
NOTIFIED_COVER = CATCH_UP_INTERVAL + timedelta(minutes=15)
# A fetch that began a little less than one interval ago still covers the window; without the
# slack a tick that lands exactly one interval later would skip and wait for the next one.
SLACK = timedelta(minutes=1)
MAX_PAUSE = timedelta(hours=1)
MAX_TYPE_BACKOFF = timedelta(hours=24)
# Data that changes during the day and that the dashboard and the downstream push show first.
FREQUENT_TYPES = frozenset(
    {
        "heart-rate",
        "sleep",
        "steps",
        "distance",
        "activity-level",
        "active-energy-burned",
        "total-calories",
        "active-minutes",
        "exercise",
    }
)
FOUND: Reason = "catch_up_found"


class Diagnostics(Protocol):
    """WebhookStatusService: a round that found new records leaves a value-free event."""

    async def record(self, *reasons: Reason) -> None: ...


def _shared_failure(exc: BaseException) -> str | None:
    """Why a failure stops every type ("credentials", "google"), or None for this type only."""
    if isinstance(exc, (OAuthError, CredentialStoreError)):
        return "credentials"
    if isinstance(exc, GoogleHealthError) and exc.http_status == 401:
        return "credentials"
    if is_transient(exc) and database_conflict(exc) is None:
        return "google"
    return None


class ScheduledCatchUp:
    tick = TICK

    def __init__(
        self,
        settings: Settings,
        incremental: IncrementalSync,
        status: Diagnostics | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.incremental = incremental
        self.status = status
        self.clock = clock
        frequent = timedelta(minutes=settings.catch_up_frequent_minutes)
        other = timedelta(minutes=settings.catch_up_other_minutes)
        self.intervals = {
            name: frequent if name in FREQUENT_TYPES else other for name in incremental.specs
        }
        self._due: dict[str, datetime] = {}
        self._failures: dict[str, int] = {}
        self._pauses = 0
        self._paused_until: datetime | None = None
        # One catch-up at a time, whether a tick or a reconciliation started it.
        self._lock = asyncio.Lock()

    def within(self, data_type: str) -> timedelta:
        """How recent a covering fetch must be to skip the request."""
        return max(NOTIFIED_COVER, self.intervals[data_type]) - SLACK

    def _stagger(self, now: datetime) -> None:
        for interval in set(self.intervals.values()):
            names = sorted(name for name, value in self.intervals.items() if value == interval)
            for index, name in enumerate(names):
                self._due[name] = now + interval * index / len(names)

    def _paused(self, now: datetime) -> bool:
        return self._paused_until is not None and now < self._paused_until

    async def run(self) -> dict[str, str]:
        """The scheduler job: catch up every due type, one at a time; outcome per type."""
        now = self.clock()
        if not self._due:
            self._stagger(now)
        outcomes: dict[str, str] = {}
        if self._paused(now):
            return outcomes
        due = sorted((when, name) for name, when in self._due.items() if when <= now)
        for _, name in due:
            outcomes[name] = await self._attempt(name)
            if outcomes[name] == "paused":
                break
        return outcomes

    async def between_slices(self, data_type: str) -> None:
        """Called by a reconciliation between two windows of ``data_type``; never raises."""
        now = self.clock()
        if data_type in self.intervals and not self._paused(now):
            if self._due.get(data_type, now) <= now:
                await self._attempt(data_type)

    async def _attempt(self, data_type: str) -> str:
        async with self._lock:
            now = self.clock()
            try:
                # Neither a reconciliation's report nor its run id: this is a catch-up.
                with (
                    reporting(None),
                    provenance(source="catch-up", context_id=new_context_id("catch-up")),
                ):
                    stats = await self.incremental.catch_up(
                        data_type,
                        wait=False,
                        within=self.within(data_type),
                        prepare=self.incremental.source.warm_up,
                    )
            except TypeBusy:
                event(logger, "SCHEDULED_CATCH_UP_BUSY", data_type=data_type)
                return "busy"
            except PaginationPaused:
                # Its pages are stored; the rest of the window follows on the next tick.
                return "partial"
            except Exception as exc:
                return self._failed(data_type, exc, now)
            self._succeeded(data_type, now)
            if stats is None:
                return "covered"
            await self._fetched(data_type, stats)
            return "fetched"

    async def _fetched(self, data_type: str, stats: StoreStats) -> None:
        records = stats.inserted + stats.updated
        if records and self.status is not None:
            await self.status.record(FOUND)
        event(logger, "SCHEDULED_CATCH_UP_COMPLETED", data_type=data_type, records=records)

    def _succeeded(self, data_type: str, now: datetime) -> None:
        self._due[data_type] = now + self.intervals[data_type]
        if self._failures.pop(data_type, 0):
            event(logger, "SCHEDULED_CATCH_UP_TYPE_RECOVERED", data_type=data_type)
        if self._pauses:
            self._pauses, self._paused_until = 0, None
            event(logger, "SCHEDULED_CATCH_UP_RESUMED")

    def _failed(self, data_type: str, exc: Exception, now: datetime) -> str:
        fields = {"error_type": type(exc).__name__, "error_code": error_code(exc)}
        reason = _shared_failure(exc)
        if reason is not None:
            self._pauses += 1
            delay = min(TICK * 2 ** (self._pauses - 1), MAX_PAUSE)
            self._paused_until = now + delay
            if self._pauses == 1:
                logger.warning(
                    "Scheduled catch-up paused",
                    extra={
                        "event": "SCHEDULED_CATCH_UP_PAUSED",
                        "reason": reason,
                        "data_type": data_type,
                        **fields,
                    },
                )
            return "paused"
        failures = self._failures[data_type] = self._failures.get(data_type, 0) + 1
        self._due[data_type] = now + min(self.intervals[data_type] * 2**failures, MAX_TYPE_BACKOFF)
        if failures == 1:
            logger.warning(
                "Scheduled catch-up of a data type failed",
                extra={"event": "SCHEDULED_CATCH_UP_FAILED", "data_type": data_type, **fields},
            )
        return "failed"
