"""Log privacy test helpers: a private marker in a health record, and what leaves the logs.

``capture_logs()`` sees the production stdout handler and every record as the call site made
it; ``assert_clean()`` checks that neither the marker nor a traceback left.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from identity_records import STEPS, fitbit, steps
from sqlalchemy import select

from app.core.config import Settings
from app.core.logging import configure_logging
from app.google_health.client import Page
from app.models.state import BackfillJob
from app.services.providers.ingestion import GoogleSource
from app.sync.backfill import BackfillService

MARKER = "synthetic-health-marker-7f3a"
WINDOW_START = datetime.now(UTC).replace(microsecond=0) - timedelta(days=3)


class RecordsAsMade(logging.Handler):
    """Every record as the call site made it, exception text included: it runs before the
    production handler's filter (pytest re-adds its own handlers after it for each phase)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(f"{self.format(record)}\n{vars(record)!r}")


def capture_logs():
    """What leaves through logging: the production stdout handler, and every record as made."""
    stdout = io.StringIO()
    real, sys.stdout = sys.stdout, stdout
    try:
        configure_logging("INFO")
    finally:
        sys.stdout = real
    as_made = RecordsAsMade()
    logging.getLogger().handlers.insert(0, as_made)

    class Emitted:
        def text(self) -> str:
            return "\n".join([*as_made.texts, stdout.getvalue()])

        def lines(self) -> list[dict]:
            return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

        def events(self, name: str) -> list[dict]:
            return [line for line in self.lines() if line.get("event") == name]

    return Emitted()


def assert_clean(emitted) -> None:
    output = emitted.text()
    assert MARKER not in output
    assert "Traceback" not in output and "'exc_info': None" in output
    assert all("exc_info" not in line for line in emitted.lines())


def health_record() -> dict:
    # A steps record from a device whose name carries the marker, plus a private value.
    record = steps(4321, start=WINDOW_START + timedelta(hours=1), source=fitbit(MARKER))
    record["steps"]["note"] = MARKER
    return record


class OnePageClient:
    def __init__(self, record: dict) -> None:
        self.record = record

    async def list_data_points(self, spec, start, end, page_token=None):
        return Page([self.record], None)

    async def keep_after(self, responses, failure, *, closing=()):
        return None


def backfill_service(sessions) -> BackfillService:
    settings = Settings(backfill_start_date=WINDOW_START.date(), backfill_window_days=31)
    return BackfillService(
        settings, sessions, GoogleSource(OnePageClient(health_record())), {"steps": STEPS}
    )


async def backfill_job(sessions) -> BackfillJob:
    async with sessions() as session:
        return (await session.scalars(select(BackfillJob))).one()
