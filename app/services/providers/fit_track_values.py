"""FIT track units and optional-value validation; no source values in errors or logs."""

import math

COORDINATE_DIGITS = 7

# field -> (name, divisor, offset, minimum, maximum). Later (enhanced) fields replace
# earlier legacy ones only when valid. Degrees = semicircles * 180 / 2**31.
RECORD_FIELDS = {
    0: ("latitude", 2**31 / 180, 0, -90, 90),
    1: ("longitude", 2**31 / 180, 0, -180, 180),
    2: ("altitude", 5, -500, -500, 0xFFFE / 5 - 500),
    3: ("heart_rate", 1, 0, 1, 254),
    4: ("cadence", 1, 0, 0, 254),
    5: ("distance", 100, 0, 0, 0xFFFFFFFE / 100),
    6: ("speed", 1000, 0, 0, 0xFFFE / 1000),
    7: ("power", 1, 0, 0, 65534),
    13: ("temperature", 1, 0, -128, 126),
    73: ("speed", 1000, 0, 0, 0xFFFFFFFE / 1000),
    78: ("altitude", 5, -500, -500, 0xFFFFFFFE / 5 - 500),
}
LAP_FIELDS = {
    2: ("start_time", 1, 0, 0, 0xFFFFFFFE),
    7: ("elapsed_seconds", 1000, 0, 0, 0xFFFFFFFE / 1000),
    8: ("moving_seconds", 1000, 0, 0, 0xFFFFFFFE / 1000),
    9: ("distance", 100, 0, 0, 0xFFFFFFFE / 100),
    13: ("average_speed", 1000, 0, 0, 0xFFFE / 1000),
    14: ("maximum_speed", 1000, 0, 0, 0xFFFE / 1000),
    15: ("average_heart_rate", 1, 0, 1, 254),
    16: ("maximum_heart_rate", 1, 0, 1, 254),
    110: ("average_speed", 1000, 0, 0, 0xFFFFFFFE / 1000),
    111: ("maximum_speed", 1000, 0, 0, 0xFFFFFFFE / 1000),
}


def timestamp(fields: dict) -> int | None:
    value = fields.get(253)
    if isinstance(value, int) and 0 <= value < 0xFFFFFFFF:
        return value
    return None


def measurements(fields: dict, profile: dict) -> dict:
    result = {}
    for field, (name, divisor, offset, minimum, maximum) in profile.items():
        value = fields.get(field)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        # Check wire-domain bounds before scaling: a tiny negative must not underflow
        # to accepted negative zero, and a finite float cannot exceed the native range.
        if value < (minimum - offset) * divisor:
            continue
        if maximum is not None and value > (maximum - offset) * divisor:
            continue
        # One division of an exact integer keeps decimal values short (510.2, not 510.20…05).
        result[name] = (value + offset * divisor) / divisor
    return result


def sample(fields: dict) -> dict | None:
    moment = timestamp(fields)
    if moment is None:
        return None
    values = measurements(fields, RECORD_FIELDS)
    # A position is a pair. Wire (0, 0) is how some recorders write "no fix" instead of
    # the invalid sentinel; only that exact pair is dropped. The synthetic generator never
    # emits it, and every other position around 0°/0° (its invented region) is kept.
    if "latitude" not in values or "longitude" not in values or fields.get(0) == fields.get(1) == 0:
        values.pop("latitude", None)
        values.pop("longitude", None)
    else:
        # 1e-7 degrees (about 1 cm) is the semicircle resolution; it halves package size.
        values["latitude"] = round(values["latitude"], COORDINATE_DIGITS)
        values["longitude"] = round(values["longitude"], COORDINATE_DIGITS)
    return {"time": moment, **values}


def lap(fields: dict) -> dict:
    values = measurements(fields, LAP_FIELDS)
    if (moment := timestamp(fields)) is not None:
        values["time"] = moment
    return values


def timer_event(fields: dict) -> dict | None:
    # FIT event=timer; start, stop, stop_all, stop_disable, stop_disable_all.
    moment, action = timestamp(fields), fields.get(1)
    if fields.get(0) != 0 or moment is None or action not in (0, 1, 4, 8, 9):
        return None
    return {"time": moment, "action": "resume" if action == 0 else "pause"}
