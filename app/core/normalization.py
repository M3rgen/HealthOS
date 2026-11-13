"""Source-neutral normalization: UTC timestamps, units, digests and SQL numeric bounds."""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

from sqlalchemy import Numeric
from sqlalchemy.orm import DeclarativeBase

# Decimal can reject a syntactically numeric exponent beyond its implementation limit.
# Ordinary text in generic payload attributes is still not a numeric measurement.
_DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def number(value: object) -> Decimal | None:
    """Ignore missing/text fields, but reject unrepresentable numeric measurements.

    Write callers catch DecimalException around normalization only. Read paths retain
    common.number's permissive parsing and their existing finite arithmetic checks.
    """
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        if _DECIMAL_TEXT.fullmatch(text.strip()):
            raise InvalidOperation("Unrepresentable source number") from None
        return None
    if not parsed.is_finite():
        raise InvalidOperation("Nonfinite source number")
    return parsed


def duration_seconds(value: object) -> Decimal | None:
    return number(value[:-1] if isinstance(value, str) and value.endswith("s") else value)


def first_number(
    payload: dict,
    candidates: tuple[str, ...],
    *,
    parse: Callable[[object], Decimal | None] = number,
) -> Decimal | None:
    """Select the first parsed field, preserving the caller's numeric error policy."""
    for name in candidates:
        parsed = parse(payload.get(name))
        if parsed is not None:
            return parsed
    return None


def from_milli(value: Decimal) -> Decimal:
    """Shift the unit exactly, without context rounding before SQL scale rounding."""
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise InvalidOperation("Nonfinite source number")
    return Decimal((sign, digits, exponent - 3))


def _fit(value: Decimal, precision: int, scale: int) -> Decimal | None:
    """Round as PostgreSQL Numeric(p, s), or None on overflow including rounding carry.

    Bound the quantize context by the schema, never by an untrusted source exponent.
    """
    integer_digits = precision - scale
    if value and value.adjusted() >= integer_digits:
        return None
    with localcontext() as context:
        context.prec = precision + 1
        rounded = value.quantize(Decimal((0, (1,), -scale)), rounding=ROUND_HALF_UP)
    return None if rounded and rounded.adjusted() >= integer_digits else rounded


def prepare_numeric(model: type[DeclarativeBase], values: dict) -> None:
    """Fit every Numeric column; only a required column's overflow rejects the record.

    An optional value that cannot be stored becomes NULL, so a measurement is never
    lost for its extra metric. Send the rounded Decimal too: PostgreSQL's wire numeric
    has exponent limits even when a tiny source number would round to zero.
    """
    for column in model.__table__.columns:
        kind = column.type
        if not isinstance(kind, Numeric):
            continue
        value = values.get(column.name)
        if value is None:
            continue
        precision, scale = kind.precision, kind.scale
        if precision is None or scale is None:
            raise RuntimeError("Numeric column requires explicit precision and scale")
        if not value.is_finite():
            raise InvalidOperation("Nonfinite normalized number")
        rounded = _fit(value, precision, scale)
        if rounded is None and not column.nullable:
            raise InvalidOperation("Numeric column overflow")
        values[column.name] = rounded


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (ValueError, OverflowError):
        # A valid local datetime can overflow Python's year range when converted to UTC.
        return None


def instant_ms(value: Any) -> datetime | None:
    """An epoch in milliseconds; flags and unrepresentable instants are absent."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def civil_bounds(day: date, zone: tzinfo = UTC) -> tuple[datetime, datetime]:
    """Half-open UTC bounds of a civil day, including 23/25-hour timezone days."""
    return (
        datetime.combine(day, time.min, zone).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC),
    )
