"""The common shape of an Apple Health record, whichever way it came in.

Both ways in produce ``AppleRecord`` values: the streaming ``export.xml`` parser
(app/apple/export_xml.py, channel ``export``) and the Health Auto Export payload parser
(app/apple/hae.py, channel ``hae``). The archive (app/services/apple_archive.py) stores each
one in ``apple_raw_records`` and normalization (app/services/apple_normalize.py) turns it into
rows of the shared normalized tables with ``provider = 'APPLE'``.

``key`` is the record's identity inside its channel and kind; ``body`` is the record as
received (attributes of an XML element, or a JSON data point), JSON-safe. A record received
again with the same body is the same version (``body_sha256``); the same key with another body
is a newer version (Health Auto Export re-sends a running day total with a larger value).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

EXPORT, HAE = "export", "hae"
CHANNELS = (EXPORT, HAE)

# Kinds (``apple_raw_records.kind``).
QUANTITY = "quantity"  # export.xml <Record type="HKQuantityTypeIdentifier...">
CATEGORY = "category"  # export.xml <Record type="HKCategoryTypeIdentifier...">
RECORD = "record"  # export.xml <Record> of any other type
WORKOUT = "workout"  # export.xml <Workout>
ACTIVITY_SUMMARY = "activity-summary"  # export.xml <ActivitySummary>
HAE_METRIC = "hae-metric"  # Health Auto Export data.metrics[].data[] point
HAE_WORKOUT = "hae-workout"  # Health Auto Export data.workouts[] item
KINDS = (QUANTITY, CATEGORY, RECORD, WORKOUT, ACTIVITY_SUMMARY, HAE_METRIC, HAE_WORKOUT)

# Field limits of apple_raw_records (app/models/apple.py).
TYPE_LENGTH = 128
TEXT_LENGTH = 255


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, non-ASCII kept: the same content is the same text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_digest(body: Any) -> str:
    """SHA-256 of a body's canonical JSON."""
    return sha256_text(canonical_json(body))


def identity_key(*parts: object) -> str:
    """SHA-256 of the identity fields of a record (``None`` and text kept apart)."""
    return sha256_text(canonical_json([None if part is None else str(part) for part in parts]))


def clip(value: object, length: int = TEXT_LENGTH) -> str | None:
    """A column-safe text: stripped, None when empty, cut to ``length`` characters."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:length] if text else None


# ``2024-02-06 14:30:00 -0800`` (export.xml and Health Auto Export); ISO 8601 is accepted too.
_APPLE_DATE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)"
    r"\s*(?P<zone>Z|[+-]\d{2}:?\d{2})?"
)


def parse_apple_date(value: object) -> datetime | None:
    """An Apple timestamp as an aware UTC datetime; None when it is not one. A timestamp
    without an offset is not guessed (None)."""
    if not isinstance(value, str):
        return None
    match = _APPLE_DATE.fullmatch(value.strip())
    if match is None or match.group("zone") is None:
        return None
    zone = match.group("zone")
    if zone != "Z" and ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        parsed = datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}{zone}")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


_DEVICE_FIELD = re.compile(r"(?:^|,)\s*(name|model|manufacturer|hardware|software):([^,>]*)")


def device_fields(device: object) -> dict[str, str]:
    """The fields of export.xml's ``device`` attribute (``<<HKDevice: 0x…>, name:Apple Watch,
    manufacturer:Apple Inc., model:Watch, hardware:Watch6,1, software:10.1>``)."""
    if not isinstance(device, str):
        return {}
    found: dict[str, str] = {}
    for name, value in _DEVICE_FIELD.findall(device):
        text = value.strip()
        if text and name not in found:
            found[name] = text
    return found


def device_name(device: object) -> str | None:
    """The device's model name from export.xml's ``device`` attribute (``Apple Watch``)."""
    return clip(device_fields(device).get("name"))


@dataclass(frozen=True)
class AppleRecord:
    """One Apple Health record as received (see the module docstring).

    ``start``/``end`` are aware UTC datetimes (None when the record has no time, or the
    time could not be read); ``source_name`` is Health's source (``sourceName``, or Health
    Auto Export's ``source``); ``device`` the device model when export.xml names one.
    """

    channel: str
    kind: str
    type: str
    key: str
    body: dict[str, Any]
    start: datetime | None = None
    end: datetime | None = None
    source_name: str | None = None
    device: str | None = None
    digest: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown Apple channel: {self.channel}")
        if self.kind not in KINDS:
            raise ValueError(f"unknown Apple record kind: {self.kind}")
        if not self.digest:
            object.__setattr__(self, "digest", body_digest(self.body))

    @property
    def body_sha256(self) -> str:
        return self.digest
