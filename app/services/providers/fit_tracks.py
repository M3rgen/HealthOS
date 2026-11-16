"""Deterministic, bounded FIT-to-track packages; raw FIT always remains authoritative.

Method 1 package: three zlib-compressed JSON objects of equal-length columns (one array
per field, ``null`` for a missing value) plus a small summary. Every time is whole
seconds relative to the summary's ``start_time``; a channel without any valid value is
omitted, so a reader never draws an empty series.
"""

import io
import json
import math
import zlib
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from app.core.fit import MAX_FIT_RECORDS

from .fit_decode import FitError, messages
from .fit_records import EPOCH
from .fit_track_summary import TrackSummary
from .fit_track_values import lap, sample, timer_event

METHOD_VERSION = 1
SAMPLE_COLUMNS = (
    "time",
    "latitude",
    "longitude",
    "altitude",
    "speed",
    "heart_rate",
    "distance",
    "cadence",
    "power",
    "temperature",
)
LAP_COLUMNS = (
    "start_time",
    "time",
    "elapsed_seconds",
    "moving_seconds",
    "distance",
    "average_speed",
    "maximum_speed",
    "average_heart_rate",
    "maximum_heart_rate",
)
EVENT_COLUMNS = ("time", "action")
TIME_COLUMNS = frozenset({"time", "start_time"})
ACTIONS = ("resume", "pause")
CHUNK = 1024


def _plain(value: float) -> int | float | None:
    if math.isnan(value):
        return None
    return int(value) if value.is_integer() else value


class Columns:
    """Equal-length float64 columns (NaN = missing): 8 bytes per value, never row dicts."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.data = {name: array("d") for name in names}
        self.count = 0

    def append(self, values: dict) -> None:
        for name, column in self.data.items():
            value = values.get(name)
            if name == "action":
                value = ACTIONS.index(value)
            column.append(math.nan if value is None else value)
        self.count += 1

    def times(self) -> Iterable[float]:
        for name in TIME_COLUMNS & self.data.keys():
            yield from (value for value in self.data[name] if not math.isnan(value))

    def pack(self, origin: float) -> bytes:
        compressor, output = zlib.compressobj(), io.BytesIO()
        written = 0
        output.write(compressor.compress(b"{"))
        for name, column in self.data.items():
            if name not in TIME_COLUMNS and all(math.isnan(value) for value in column):
                continue  # Absent channel: no all-null arrays for missing sensors.
            shift = origin if name in TIME_COLUMNS else 0
            head = ("," if written else "") + json.dumps(name) + ":["
            output.write(compressor.compress(head.encode()))
            for start in range(0, len(column), CHUNK):
                chunk = [_plain(value - shift) for value in column[start : start + CHUNK]]
                if name == "action":
                    chunk = [ACTIONS[int(value)] for value in chunk if value is not None]
                text = json.dumps(chunk, separators=(",", ":"), allow_nan=False)[1:-1]
                output.write(compressor.compress(("," if start else "").encode() + text.encode()))
            output.write(compressor.compress(b"]"))
            written += 1
        output.write(compressor.compress(b"}"))
        output.write(compressor.flush())
        return output.getvalue()


@dataclass(frozen=True)
class TrackPackage:
    samples: bytes
    laps: bytes
    events: bytes
    summary: dict
    sample_count: int


def derive_track(data: bytes) -> TrackPackage:
    """Times are FIT-epoch UTC seconds on the wire; measurements use SI (HR in bpm).

    The activity type is not part of a package: readers take it from the existing
    workout record. No route shape, speed or coordinate is used to guess a sport.
    Empty files produce empty tracks; a damaged file raises before anything is packed.
    """
    samples, laps, events = Columns(SAMPLE_COLUMNS), Columns(LAP_COLUMNS), Columns(EVENT_COLUMNS)
    summary = TrackSummary()
    count = 0
    for kind, fields in messages(data):
        if kind not in (19, 20, 21):
            continue
        count += 1
        if count > MAX_FIT_RECORDS:
            raise FitError("FIT iz kayıt sınırı aşıldı / FIT track record limit exceeded")
        if kind == 20:
            point = sample(fields)
            if point is not None and summary.sample(point):
                samples.append(point)
        elif kind == 19:
            if values := lap(fields):
                laps.append(values)
        elif (event := timer_event(fields)) is not None:
            events.append(event)
            summary.event(event)
    # Without records, laps/events are relative to their own earliest time.
    origin = summary.first
    if origin is None:
        origin = min((*laps.times(), *events.times()), default=0)
    result = summary.result()
    empty = not (samples.count or laps.count or events.count)
    result["start_time"] = None if empty else (EPOCH + timedelta(seconds=origin)).isoformat()
    return TrackPackage(
        samples.pack(origin), laps.pack(origin), events.pack(origin), result, samples.count
    )
