"""JSON primitives for health tool results, independent of the HTTP framework."""

import json
from datetime import date
from decimal import Decimal
from enum import Enum
from uuid import UUID


def _default(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        exponent = value.as_tuple().exponent
        return int(value) if isinstance(exponent, int) and exponent >= 0 else float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported JSON type: {type(value).__name__}")


def json_values(value: dict) -> dict:
    return json.loads(json.dumps(value, default=_default, ensure_ascii=False))
