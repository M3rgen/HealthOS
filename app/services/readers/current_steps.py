"""Read a step measurement from its current payload, never a retained normalized row."""

from datetime import datetime

from app.core.normalization import first_number
from app.core.records import RecordType
from app.parsers.common import number, payload_for, temporal
from app.services.shared.finite_math import finite_number


def current_steps(record: dict) -> tuple[datetime, datetime, float] | None:
    """Use normalization's field aliases and time parser, preserving measured zero.

    A correction that cannot normalize deliberately leaves the old Steps row in place.
    Its count and interval are historical evidence, so readers must validate both against
    the current raw payload. Eligibility (identity, quarantine, deletion) stays with the
    caller's query; this function only establishes whether a measurement exists.
    """
    spec = RecordType("steps")
    parsed = first_number(payload_for(spec, record), ("count", "countSum", "steps"), parse=number)
    # Check the source Decimal before float conversion and independently of the
    # retained SQL count. Signed zero is valid; negative aliases never fall through.
    if parsed is None or not parsed.is_finite():
        return None
    if parsed < 0:
        return None
    count = finite_number(parsed)
    left, right, _ = temporal(spec, record)
    if count is None or left is None or right is None or right <= left:
        return None
    return left, right, count
