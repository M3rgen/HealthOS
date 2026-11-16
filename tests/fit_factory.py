"""Synthetic FIT activity writer. No real device, location or health data.

CRC uses the FIT SDK nibble table, independently of the production bitwise decoder.
"""

import itertools
import struct
from datetime import UTC, datetime

EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
START = datetime(2026, 11, 1, 10, tzinfo=UTC)
STAMP = int((START - EPOCH).total_seconds())
CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)


def checksum(data):
    crc = 0
    for byte in data:
        for nibble in (byte & 15, byte >> 4):
            crc = (crc >> 4) ^ CRC_TABLE[crc & 15] ^ CRC_TABLE[nibble]
    return crc


def fit_file(messages, *, header_size=14, protocol=0x20, header_crc=True):
    header = struct.pack("<BBHI4s", header_size, protocol, 2200, len(messages), b".FIT")
    if header_size == 14:
        header += struct.pack("<H", checksum(header) if header_crc else 0)
    content = header + messages
    return content + struct.pack("<H", checksum(content))


def definition(local, global_number, fields, *, endian="<", developer=()):
    header = 0x40 | local | (0x20 if developer else 0)
    output = bytes([header, 0, int(endian == ">")])
    output += struct.pack(endian + "H", global_number) + bytes([len(fields)])
    output += b"".join(bytes(field) for field in fields)
    if developer:
        output += bytes([len(developer)]) + b"".join(bytes(field) for field in developer)
    return output


def activity_messages(
    *,
    bpm=120,
    serial=4242,
    endian="<",
    developer=False,
    distance=250000,
    manufacturer=1,
    elapsed=600000,
    duration=590500,
    sport=1,
):
    data = definition(
        0, 0, [(0, 1, 0), (1, 2, 0x84), (2, 2, 0x84), (3, 4, 0x8C), (4, 4, 0x86)], endian=endian
    )
    data += b"\x00" + struct.pack(endian + "BHHII", 4, manufacturer, 123, serial, STAMP)
    data += definition(
        1,
        18,
        [
            (253, 4, 0x86),
            (2, 4, 0x86),
            (5, 1, 0),
            (6, 1, 0),
            (7, 4, 0x86),
            (8, 4, 0x86),
            (9, 4, 0x86),
            (11, 2, 0x84),
        ],
        endian=endian,
    )
    data += b"\x01" + struct.pack(
        endian + "IIBBIIIH",
        STAMP + elapsed // 1000,
        STAMP,
        sport,
        0,
        elapsed,
        duration,
        distance,
        150,
    )
    data += definition(
        2,
        20,
        [(253, 4, 0x86), (3, 1, 2)],
        endian=endian,
        developer=[(0, 3, 0)] if developer else (),
    )
    data += b"\x02" + struct.pack(endian + "IB", STAMP, bpm)
    if developer:
        data += b"abc"
    data += bytes([0x80 | (2 << 5) | ((STAMP + 1) & 31), bpm + 1])
    if developer:
        data += b"def"
    return data


def activity(**kwargs):
    return fit_file(activity_messages(**kwargs))


def timestamp_message(timestamp, *, endian="<"):
    """An unprojected global message that still updates the shared time base."""
    return (
        definition(3, 999, [(253, 4, 0x86)], endian=endian)
        + b"\x03"
        + struct.pack(endian + "I", timestamp)
    )


# The invented region: a circle of this radius (degrees) around 0°/0°, in the open ocean.
TRACK_RADIUS = 0.005
TRACK_BOUNDS = (-0.005001, -0.005001, 0.005001, 0.005001)
TRACK_SPEED = 12.0


def track_points(count=700, seed=1, intervals=None, pauses=()):
    """A seeded circle around zero in the ocean; all values are deliberately invented.

    Times are relative seconds, pauses are pairs of relative seconds. Positions move
    along the circle by the distance covered at a constant speed, so the odometer,
    speed and coordinate distance agree; the exact pair (0, 0) never occurs. A
    triangular elevation profile keeps summaries independently predictable.
    """
    import math
    import random

    rng = random.Random(seed)
    radius = math.radians(TRACK_RADIUS) * 6_371_000
    elapsed, distance = 0, 0.0
    for index in range(count):
        step = (
            (intervals[index % len(intervals)] if intervals else rng.randint(1, 25)) if index else 0
        )
        active = step - sum(
            max(0, min(elapsed + step, end) - max(elapsed, start)) for start, end in pauses
        )
        elapsed += step
        distance += TRACK_SPEED * active
        angle = distance / radius
        yield {
            "time": elapsed,
            "latitude": TRACK_RADIUS * math.sin(angle),
            "longitude": TRACK_RADIUS * math.cos(angle),
            "speed": TRACK_SPEED,
            "altitude": 10 + min(index % 20, 20 - index % 20),
            "heart_rate": 120 + rng.randrange(20),
            "distance": distance,
            "cadence": 80,
            "power": 200,
            "temperature": 18,
        }


def track_messages(
    points, *, enhanced=True, optional=True, pauses=(), overrides=None, endian="<", splits=()
):
    """Write records, timer events, laps and a sport session without recorded fixtures.

    ``overrides`` changes wire values by record index, for missing/sentinel test cases.
    ``splits`` (relative seconds) cut the single lap into several; pauses are then ignored
    in lap timer values.
    """
    fields = [
        (253, 4, 0x86),
        (0, 4, 0x85),
        (1, 4, 0x85),
        (3, 1, 2),
        (5, 4, 0x86),
        (73 if enhanced else 6, 4 if enhanced else 2, 0x86 if enhanced else 0x84),
        (78 if enhanced else 2, 4 if enhanced else 2, 0x86 if enhanced else 0x84),
        (13, 1, 1),
    ]
    if optional:
        fields += [(4, 1, 2), (7, 2, 0x84)]
    codes = {0x86: "I", 0x85: "i", 2: "B", 0x84: "H", 1: "b"}
    data = bytearray(activity_messages()[:35])  # Synthetic file identity only.
    data += definition(2, 20, fields, endian=endian)
    data += definition(3, 21, [(253, 4, 0x86), (0, 1, 0), (1, 1, 0)], endian=endian)
    events = sorted(
        [(time, action) for start, stop in pauses for time, action in ((start, 1), (stop, 0))]
    )
    event_index, last = 0, None
    for index, point in enumerate(points):
        while event_index < len(events) and events[event_index][0] <= point["time"]:
            moment, action = events[event_index]
            data += b"\x03" + struct.pack(endian + "IBB", STAMP + moment, 0, action)
            event_index += 1
        values = {
            253: STAMP + point["time"],
            0: round(point["latitude"] * 2**31 / 180),
            1: round(point["longitude"] * 2**31 / 180),
            3: point["heart_rate"],
            5: round(point["distance"] * 100),
            4: point["cadence"],
            7: point["power"],
            13: point["temperature"],
            73 if enhanced else 6: round(point["speed"] * 1000),
            78 if enhanced else 2: round((point["altitude"] + 500) * 5),
        }
        values.update((overrides or {}).get(index, {}))
        data += b"\x02" + b"".join(
            struct.pack(endian + codes[base], values[number]) for number, _, base in fields
        )
        last = point
    if last is not None:
        data += track_lap_session(last, pauses, endian, splits)
    return bytes(data)


def track_lap_session(last, pauses, endian, splits=()):
    elapsed = last["time"]
    moving = elapsed - sum(max(0, min(elapsed, end) - start) for start, end in pauses)
    edges = [0, *splits, elapsed] if splits else [0, elapsed]
    lap_fields = [
        (253, 4, 0x86),
        (2, 4, 0x86),
        (7, 4, 0x86),
        (8, 4, 0x86),
        (9, 4, 0x86),
        (13, 2, 0x84),
        (14, 2, 0x84),
        (15, 1, 2),
        (16, 1, 2),
    ]
    data = definition(4, 19, lap_fields, endian=endian)
    for begin, end in itertools.pairwise(edges):
        share = (end - begin) / elapsed if elapsed else 1
        data += b"\x04" + struct.pack(
            endian + "IIIIIHHBB",
            STAMP + end,
            STAMP + begin,
            (end - begin) * 1000,
            (end - begin) * 1000 if splits else moving * 1000,
            round(last["distance"] * share * 100),
            12000,
            12000,
            125,
            139,
        )
    session_fields = [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0), (7, 4, 0x86), (8, 4, 0x86)]
    data += definition(1, 18, session_fields, endian=endian)
    data += b"\x01" + struct.pack(
        endian + "IIBII", STAMP + elapsed, STAMP, 1, elapsed * 1000, moving * 1000
    )
    return data


def track_activity(count=700, seed=1, **options):
    intervals, pauses = options.pop("intervals", None), options.get("pauses", ())
    points = track_points(count, seed, intervals, pauses)
    return fit_file(track_messages(points, **options))
