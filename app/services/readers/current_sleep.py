"""Read the current sleep interval without reviving retained normalized timestamps."""

from datetime import datetime

from sqlalchemy import SQLColumnExpression

from app.core.records import RecordType
from app.parsers.common import payload_for, temporal


def sleep_wake_window(
    end_column: SQLColumnExpression[datetime | None], start: datetime, stop: datetime
):
    """SQL candidates for a civil wake-date window, shared by readers and their budget.

    A missing start can still have an attributable wake-date candidate; the payload
    validator below rejects its measurement. A missing end has no wake date and cannot
    be placed in a window using an older normalized end. Keep the same candidate set
    for dashboard, source presence and HTTP read limits before loading payloads.
    """
    return end_column >= start, end_column < stop


def current_sleep(record: dict) -> tuple[datetime, datetime, dict] | None:
    """Validate the current source interval using the ingestion parser.

    Storage preserves an old SleepSession when a correction cannot normalize. That row
    remains archive evidence, but cannot supply missing dates for the current payload.
    Eligibility stays with the caller; duration, awake and overlap rules stay with the
    sleep estimator. A valid interval alone still supports its awake-unknown upper bound.
    """
    spec = RecordType("sleep")
    start, end, _ = temporal(spec, record)
    if start is None or end is None or end <= start:
        return None
    return start, end, payload_for(spec, record)
